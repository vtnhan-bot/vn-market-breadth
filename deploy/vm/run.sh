#!/bin/bash
# VN daily market-breadth dashboard (VM). Mon-Fri 15:15 + Tue-Sat 07:30 ICT.
# Full pipeline runs locally; cache stays on local disk (no GCS restore/persist).
# Publishes index.html + intraday/*.csv ONLY on a successful pipeline.
set -o pipefail
APP=/opt/market-breadth
export TZ=Asia/Ho_Chi_Minh          # match the container ENV; date.today() must be ICT
set -a; source "$APP/.env"; set +a
export PATH="/snap/bin:$PATH"
export PYTHONUNBUFFERED=1
GSUTIL=/snap/bin/gsutil
PY="$APP/venv/bin/python3"
BUCKET=vn-market-breadth
cd "$APP"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
echo "[$(ts)] === VN Market Breadth (VM) start ==="

mkdir -p data logs audit_logs cache/rs_history cache/rs_history_crypto cache/archive

# Full daily pipeline (downloader -> universe -> RS 3T -> RS crypto -> breadth).
# market_breadth.py also uploads intraday_breadth.json to gs://$BUCKET via the VM SA.
rc=0
"$PY" -B run_daily_update.py || rc=$?

if [ "$rc" -ne 0 ]; then
  echo "[$(ts)] ERROR: run_daily_update exited $rc - NOT publishing (leaving last-good live)"
  exit "$rc"
fi

# no-cache (NOT no-store): the browser must revalidate with GCS before reusing a
# stored copy, so a changed dashboard is always picked up -- same freshness
# contract as before. no-store additionally forbade STORING it, which forced a
# full 800KB re-download of index.html on every single page load even though the
# daily run only rebuilds it twice a day. GCS honours If-None-Match and answers
# an unchanged index.html with a 304 (0 bytes, ~0.17s vs ~0.52s).
NOCACHE="Cache-Control:no-cache, must-revalidate"
fail=0

# Best-effort Cloudflare R2 mirror of the artifact just published to GCS (dual-publish).
# r2_publish.py self-disables (exit 0, no-op) until R2_* env vars exist, so this is
# inert pre-cutover; the trailing `|| true` guarantees it can never affect $fail.
r2pub() { # r2pub <local_path> <key> <content_type>
  "$PY" -B r2_publish.py "$1" "$2" "$3" \
    && echo "[$(ts)] R2: published $2" \
    || echo "[$(ts)] R2: skipped/failed $2 (non-fatal)"
}

OUT="$APP/market_breadth.html"

# Transactional publish: data objects FIRST, index.html LAST. The dashboard's HTML
# must never go live ahead of the data objects it references, and a partial publish
# must not masquerade as success -- EVERY gsutil cp failure sets fail=1 (the rc-gate
# above already proved the build itself succeeded). The R2 mirror stays best-effort
# (`|| true`) and can never flip $fail.

# combined_dataset.csv (intraday SMA seed).
LATEST_CDS=$(ls -1 "$APP"/data/*/combined_dataset.csv 2>/dev/null | sort | tail -n1)
if [ -n "$LATEST_CDS" ] && [ -s "$LATEST_CDS" ]; then
  if "$GSUTIL" -h "$NOCACHE" -h "Content-Type:text/csv" cp "$LATEST_CDS" "gs://$BUCKET/intraday/combined_dataset.csv"; then
    echo "[$(ts)] persisted intraday/combined_dataset.csv from $LATEST_CDS"
    r2pub "$LATEST_CDS" "intraday/combined_dataset.csv" "text/csv" || true
  else
    echo "[$(ts)] ERROR: combined_dataset.csv publish failed"; fail=1
  fi
else
  echo "[$(ts)] WARN: no combined_dataset.csv under data/*/ - skipping"
fi
for f in rs_matrix_3T.csv rs_matrix_crypto.csv; do
  if [ -s "$APP/$f" ]; then
    if "$GSUTIL" -h "$NOCACHE" -h "Content-Type:text/csv" cp "$APP/$f" "gs://$BUCKET/intraday/$f"; then
      echo "[$(ts)] persisted intraday/$f"
      r2pub "$APP/$f" "intraday/$f" "text/csv" || true
    else
      echo "[$(ts)] ERROR: $f publish failed"; fail=1
    fi
  fi
done

# index.html LAST, and only once every data object above published cleanly, so the
# page never references data that failed to go live.
if [ "$fail" -ne 0 ]; then
  echo "[$(ts)] ERROR: a data object failed to publish - NOT publishing index.html (keeping the live page consistent with its data)"
elif [ -s "$OUT" ]; then
  if "$GSUTIL" -h "$NOCACHE" -h "Content-Type:text/html" cp "$OUT" "gs://$BUCKET/index.html"; then
    echo "[$(ts)] published index.html"
    r2pub "$OUT" "index.html" "text/html" || true
  else
    echo "[$(ts)] ERROR: index.html publish failed"; fail=1
  fi
else
  echo "[$(ts)] ERROR: no market_breadth.html on disk to publish"; fail=1
fi

echo "[$(ts)] === done (build rc=$rc, publish fail=$fail) | https://storage.googleapis.com/$BUCKET/index.html ==="
exit "$fail"
