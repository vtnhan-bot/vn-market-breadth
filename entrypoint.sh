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
mkdir -p data logs audit_logs cache/rs_history cache/archive

# 3) Run the full daily pipeline:
#    eod_batch_downloader -> rs_universe_generator -> rs_matrix_3T -> market_breadth
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
