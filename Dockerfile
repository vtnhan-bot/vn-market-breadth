FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt google-cloud-storage
COPY market_breadth.py tickers.csv entrypoint.sh ./
RUN chmod +x entrypoint.sh
CMD ["bash", "entrypoint.sh"]
