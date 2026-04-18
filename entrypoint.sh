#!/bin/bash
set -e

BUCKET="vn-market-breadth"

echo "=== Vietnam Market Breadth - Cloud Run Job ==="

# Restore cache from GCS
echo "Restoring cache from GCS..."
python3 - <<'PYEOF'
from google.cloud import storage
import os, pathlib
client = storage.Client()
bucket = client.bucket("vn-market-breadth")
blobs = list(bucket.list_blobs(prefix="cache/"))
if blobs:
    pathlib.Path("./cache").mkdir(exist_ok=True)
    count = 0
    for blob in blobs:
        fname = blob.name[len("cache/"):]
        if fname:
            blob.download_to_filename(f"./cache/{fname}")
            count += 1
    print(f"Restored {count} cache files.")
else:
    print("No cache found, starting fresh.")
PYEOF

# Generate the chart
python3 market_breadth.py --no-browser

# Upload HTML to GCS
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

# Save cache back to GCS
echo "Saving cache to GCS..."
python3 - <<'PYEOF'
from google.cloud import storage
import glob, os
client = storage.Client()
bucket = client.bucket("vn-market-breadth")
files = glob.glob("./cache/*.pkl")
for f in files:
    blob = bucket.blob(f"cache/{os.path.basename(f)}")
    blob.upload_from_filename(f)
print(f"Saved {len(files)} cache files.")
PYEOF

echo "=== Done! Chart live at: https://storage.googleapis.com/vn-market-breadth/index.html ==="
