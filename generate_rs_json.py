import csv
import json
from collections import defaultdict

# Read RS data
data = defaultdict(lambda: {})
with open('rs_matrix_data.csv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        ticker = row['ticker']
        date = row['session_date']
        rs_rating = int(float(row['rs_rating']))
        daily_change = float(row['daily_change_pct'])
        
        data[ticker][date] = {
            'rs': rs_rating,
            'change': daily_change
        }

# Get the 5 most recent dates
all_dates = set()
for ticker_data in data.values():
    all_dates.update(ticker_data.keys())

sorted_dates = sorted(list(all_dates), reverse=True)[:5]
print(f"Using dates: {sorted_dates}")

# Build the heatmap data - ranked by latest RS rating
latest_date = sorted_dates[0]

# Get latest data for ranking
latest_data = []
for ticker, dates_data in data.items():
    if latest_date in dates_data:
        latest_data.append({
            'ticker': ticker,
            'rs': dates_data[latest_date]['rs'],
            'change': dates_data[latest_date]['change']
        })

# Sort by RS rating descending
latest_data.sort(key=lambda x: x['rs'], reverse=True)

# Build the final heatmap structure
heatmap_data = []
for row in latest_data:
    ticker = row['ticker']
    historical = []
    
    for date in sorted_dates:
        if date in data[ticker]:
            historical.append({
                'date': date,
                'rs': data[ticker][date]['rs'],
                'change': data[ticker][date]['change']
            })
    
    if historical:
        heatmap_data.append({
            'ticker': ticker,
            'data': historical
        })

print(f"Total tickers in heatmap: {len(heatmap_data)}")
print("\nTop 10 tickers by latest RS:")
for i, stock in enumerate(heatmap_data[:10], 1):
    print(f"{i:3}. {stock['ticker']:6} - RS: {stock['data'][0]['rs']:3}")

# Write to JSON file
with open('rs_data_all_tickers.json', 'w', encoding='utf-8') as f:
    json.dump(heatmap_data, f, indent=2)

print(f"\nGenerated rs_data_all_tickers.json")
