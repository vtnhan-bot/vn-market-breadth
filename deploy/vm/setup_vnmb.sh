#!/bin/bash
# Run ON the VM. Stages /opt/market-breadth (VN daily market-breadth) from /tmp/vnmb-src.
# User home under /home (snap gsutil requirement); app dir in /opt. Does NOT enable the timer.
set -euo pipefail
PROJECT=project-feb6df0e-9749-4925-b4e
APP=/opt/market-breadth

echo "== 1. user (home under /home for snap gsutil; app in /opt) =="
id marketbreadth &>/dev/null || sudo useradd --system --create-home --home-dir /home/marketbreadth --shell /usr/sbin/nologin marketbreadth
sudo install -d -o marketbreadth -g marketbreadth -m 0755 "$APP"

echo "== 2. sync source =="
sudo rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.venv' --exclude='cache' --exclude='data' --exclude='.env' /tmp/vnmb-src/ "$APP"/
sudo chown -R marketbreadth:marketbreadth "$APP"
sudo install -d -o marketbreadth -g marketbreadth -m 0755 "$APP/data" "$APP/logs" "$APP/audit_logs" "$APP/cache/rs_history" "$APP/cache/rs_history_crypto" "$APP/cache/archive"

echo "== 3. venv + deps (+google-cloud-storage, which is NOT in requirements.txt) =="
sudo -u marketbreadth /usr/bin/python3 -m venv "$APP/venv"
sudo -u marketbreadth "$APP/venv/bin/pip" install --upgrade pip -q
sudo -u marketbreadth "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt" google-cloud-storage

echo "== 4. seed RS-history cache once from GCS (small) =="
sudo -u marketbreadth /snap/bin/gsutil -m rsync -r gs://vn-market-breadth/cache/ "$APP/cache/" || echo "WARN: cache seed skipped/empty"

echo "== 5. .env from Secret Manager (uppercase names that actually exist) =="
{
  echo "VNSTOCK_API_KEY=$(gcloud secrets versions access latest --secret=vnstock-api-key --project=$PROJECT)"
  echo "SSI_FC_DATA_CONSUMER_ID=$(gcloud secrets versions access latest --secret=SSI_FC_DATA_CONSUMER_ID --project=$PROJECT)"
  echo "SSI_FC_DATA_CONSUMER_SECRET=$(gcloud secrets versions access latest --secret=SSI_FC_DATA_CONSUMER_SECRET --project=$PROJECT)"
  echo "TELEGRAM_BOT_TOKEN=$(gcloud secrets versions access latest --secret=TELEGRAM_BOT_TOKEN --project=$PROJECT)"
  echo "TELEGRAM_CHAT_ID=$(gcloud secrets versions access latest --secret=TELEGRAM_CHAT_ID --project=$PROJECT)"
} | sudo tee "$APP/.env" >/dev/null
sudo chmod 600 "$APP/.env"; sudo chown marketbreadth:marketbreadth "$APP/.env"

echo "== 6. run.sh =="
sudo install -o marketbreadth -g marketbreadth -m 0755 /tmp/mb-run.sh "$APP/run.sh"

echo "== 7. log file =="
sudo touch /var/log/market-breadth.log
sudo chown marketbreadth:marketbreadth /var/log/market-breadth.log

echo "== 8. systemd units (daily; timer NOT enabled yet) =="
sudo install -o root -g root -m 0644 /tmp/market-breadth.service /etc/systemd/system/market-breadth.service
sudo install -o root -g root -m 0644 /tmp/market-breadth.timer   /etc/systemd/system/market-breadth.timer
sudo systemctl daemon-reload

echo "== .env keys present: =="; sudo grep -o '^[A-Z_]*=' "$APP/.env" | tr -d '='
echo "== SETUP DONE (VN daily). Validate: sudo systemctl start market-breadth.service =="
