#!/bin/bash
# Run ON the VM AFTER the VN daily migration. Adds the intraday run-wrapper + units to the
# shared /opt/market-breadth checkout. Does NOT enable the timer.
set -euo pipefail
APP=/opt/market-breadth

echo "== 0. preconditions (shared base must exist) =="
test -x "$APP/venv/bin/python3" || { echo "FATAL: $APP/venv missing - run VN daily setup first"; exit 1; }
test -f "$APP/intraday_breadth.py" || { echo "FATAL: intraday_breadth.py missing"; exit 1; }
test -f "$APP/.env" || { echo "FATAL: .env missing"; exit 1; }
sudo grep -q '^SSI_FC_DATA_CONSUMER_ID=' "$APP/.env" && sudo grep -q '^SSI_FC_DATA_CONSUMER_SECRET=' "$APP/.env" \
  || { echo "FATAL: SSI creds missing from $APP/.env (intraday requires them)"; exit 1; }
"$APP/venv/bin/python3" -c 'import google.cloud.storage' 2>/dev/null && echo "gcs-sdk-ok" \
  || sudo -u marketbreadth "$APP/venv/bin/pip" install -q google-cloud-storage
"$APP/venv/bin/python3" -c 'import ssi_fc_data' 2>/dev/null && echo "ssi-sdk-ok" \
  || sudo -u marketbreadth "$APP/venv/bin/pip" install -q ssi-fc-data==2.2.2

echo "== 1. run_intraday.sh =="
sudo install -o marketbreadth -g marketbreadth -m 0755 /tmp/mb-run-intraday.sh "$APP/run_intraday.sh"

echo "== 2. systemd units (timer NOT enabled yet) =="
sudo install -o root -g root -m 0644 /tmp/intraday-breadth.service /etc/systemd/system/intraday-breadth.service
sudo install -o root -g root -m 0644 /tmp/intraday-breadth.timer   /etc/systemd/system/intraday-breadth.timer
sudo systemctl daemon-reload

echo "== SETUP DONE (VN intraday). Validate (dry-run, no publish):"
echo "   sudo -u marketbreadth env INTRADAY_FORCE=1 INTRADAY_DRY_RUN=1 $APP/run_intraday.sh"
