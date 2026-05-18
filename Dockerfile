FROM python:3.11-slim

# System deps for pandas/numpy compiled wheels & curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Ho_Chi_Minh
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt google-cloud-storage

# Pipeline scripts (orchestrator -> step scripts)
COPY run_daily_update.py \
     eod_batch_downloader.py \
     rs_universe_generator.py \
     rs_matrix_3T.py \
     rs_matrix_crypto.py \
     rs_source2.py \
     market_breadth.py \
     pre_breakout.py \
     _patch_pre_breakout.py \
     _patch_us_charts.py \
     intraday_breadth.py \
     intraday_rs_3T.py \
     ./

# Bootstrap input CSVs (read by the pipeline scripts on day 1)
COPY tickers.csv \
     institutional_universe_3T.csv \
     rs_fixed_tickers.csv \
     rs_universe.csv \
     crypto_universe.csv \
     ./

# Cloud Run entrypoint
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["bash", "entrypoint.sh"]
