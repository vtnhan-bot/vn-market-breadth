#!/bin/bash
# VN intraday breadth tick (every 15 min 09:00-14:00 ICT). Reads today's combined_dataset.csv
# from LOCAL disk (produced by the daily job) and self-publishes intraday_breadth.json +
# intraday_rs_3T.json to gs://vn-market-breadth via the google-cloud-storage SDK (as the VM SA).
# The script self-gates to the real trading window and no-ops (exit 0) outside it.
set -o pipefail
APP=/opt/market-breadth
export TZ=Asia/Ho_Chi_Minh
set -a; source "$APP/.env"; set +a          # SSI_FC_DATA_CONSUMER_ID/SECRET, VNSTOCK_API_KEY
export PYTHONUNBUFFERED=1
PY="$APP/venv/bin/python3"
cd "$APP"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Prefer today's locally-produced combined_dataset.csv (no per-tick GCS download).
LATEST_COMBINED="$(ls -1t "$APP"/data/*/combined_dataset.csv 2>/dev/null | head -n1)"
if [ -n "$LATEST_COMBINED" ] && [ -s "$LATEST_COMBINED" ]; then
  export INTRADAY_LOCAL_COMBINED="$LATEST_COMBINED"
  echo "[$(ts)] intraday: using local combined_dataset $LATEST_COMBINED"
else
  echo "[$(ts)] intraday: WARN no local combined_dataset - script falls back to GCS download this tick"
fi

rc=0
"$PY" -B intraday_breadth.py || rc=$?
echo "[$(ts)] intraday tick done (rc=$rc)"
exit "$rc"
