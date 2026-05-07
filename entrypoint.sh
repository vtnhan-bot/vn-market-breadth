#!/bin/bash
set -e

BUCKET="vn-market-breadth"

echo "=== Vietnam Market Breadth - Cloud Run Job ==="
echo "Started at: $(date)"

# 1) Restore cache/ from GCS (recursive — handles nested rs_history/, archive/, etc.)
echo "Restoring cache from GCS..."
python3 - <<'PYEOF'
from google.cloud import storage
import pathlib
client = storage.Client()
bucket = client.bucket("vn-market-breadth")
blobs = list(bucket.list_blobs(prefix="cache/"))
restored = 0
for blob in blobs:
    fname = blob.name[len("cache/"):]
    if not fname or fname.endswith("/"):
        continue
    dest = pathlib.Path("./cache") / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(dest))
    restored += 1
print(f"Restored {restored} cache files.")
PYEOF

# 2) Ensure runtime directories exist
mkdir -p data logs audit_logs cache/rs_history cache/rs_history_crypto cache/archive

# 3) Run the full daily pipeline:
#    eod_batch_downloader -> rs_universe_generator -> rs_matrix_3T -> rs_matrix_crypto -> market_breadth
echo "Running daily pipeline (run_daily_update.py)..."
python3 run_daily_update.py

# 4) Upload HTML to GCS (this is what the public URL serves)
echo "Uploading chart to GCS..."
python3 - <<'PYEOF'
from google.cloud import storage
client = storage.Client()
bucket = client.bucket("vn-market-breadth")
blob = bucket.blob("index.html")
blob.cache_control = "no-cache, no-store, must-revalidate"
blob.upload_from_filename("market_breadth.html", content_type="text/html")
print("Chart uploaded: https://storage.googleapis.com/vn-market-breadth/index.html")
PYEOF

# 4b) Persist today's combined_dataset.csv + the two RS matrices to GCS so
#     local re-renders + the intraday-breadth job have today's fresh data.
#     Without this, manual `python market_breadth.py && gsutil cp index.html`
#     publishes a dashboard with stale RS heatmaps even though combined_dataset
#     is fresh.
echo "Persisting combined_dataset.csv + RS matrices to GCS..."
python3 - <<'PYEOF'
import glob, os
from google.cloud import storage
client = storage.Client()
bucket = client.bucket("vn-market-breadth")

# combined_dataset.csv → intraday/ (consumed by intraday-breadth job)
candidates = sorted(glob.glob("data/*/combined_dataset.csv"))
if candidates:
    latest = candidates[-1]
    blob = bucket.blob("intraday/combined_dataset.csv")
    blob.cache_control = "no-cache, no-store, must-revalidate"
    blob.upload_from_filename(latest, content_type="text/csv")
    print(f"Persisted intraday/combined_dataset.csv from {latest} ({os.path.getsize(latest):,} bytes)")
else:
    print("No combined_dataset.csv found — skipping")

# RS matrices → intraday/ (consumed by manual local re-renders;
# the matrices are NOT part of cache/, only the per-ticker history is)
for src, dst in [
    ("rs_matrix_3T.csv",     "intraday/rs_matrix_3T.csv"),
    ("rs_matrix_crypto.csv", "intraday/rs_matrix_crypto.csv"),
]:
    if os.path.exists(src):
        blob = bucket.blob(dst)
        blob.cache_control = "no-cache, no-store, must-revalidate"
        blob.upload_from_filename(src, content_type="text/csv")
        print(f"Persisted {dst} from {src} ({os.path.getsize(src):,} bytes)")
    else:
        print(f"Skipped {src} — not found in container")
PYEOF

# 5) Persist cache/ back to GCS (recursive — keeps daily incremental fetches fast)
echo "Saving cache to GCS..."
python3 - <<'PYEOF'
from google.cloud import storage
import os, pathlib
client = storage.Client()
bucket = client.bucket("vn-market-breadth")
saved = 0
for path in pathlib.Path("./cache").rglob("*"):
    if path.is_file():
        rel = path.relative_to("./cache").as_posix()
        bucket.blob(f"cache/{rel}").upload_from_filename(str(path))
        saved += 1
print(f"Saved {saved} cache files.")
PYEOF

echo "=== Done at $(date)  |  Live: https://storage.googleapis.com/vn-market-breadth/index.html ==="
