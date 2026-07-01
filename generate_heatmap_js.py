import csv
import json

# Read RS data
ticker_data = {}
with open('rs_matrix_data.csv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        ticker = row['ticker']
        date = row['session_date']
        
        if ticker not in ticker_data:
            ticker_data[ticker] = {}
        
        ticker_data[ticker][date] = {
            'rs': int(float(row['rs_rating'])),
            'change': float(row['daily_change_pct'])
        }

# Get the 5 most recent dates
all_dates = set()
for dates_dict in ticker_data.values():
    all_dates.update(dates_dict.keys())

sorted_dates = sorted(list(all_dates), reverse=True)[:5]
print(f"Using dates: {sorted_dates}")

# Build ranking based on latest date RS
latest_date = sorted_dates[0]
ranking = []
for ticker, dates_dict in ticker_data.items():
    if latest_date in dates_dict:
        ranking.append({
            'ticker': ticker,
            'rs': dates_dict[latest_date]['rs'],
            'change': dates_dict[latest_date]['change']
        })

# Sort by RS descending
ranking.sort(key=lambda x: x['rs'], reverse=True)

print(f"Total tickers: {len(ranking)}")
print("\nGenerating JavaScript array...")

# Build the JS code
js_lines = ["const rsHeatmapData = ["]

for i, row in enumerate(ranking):
    ticker = row['ticker']
    js_lines.append("  {")
    js_lines.append(f"    ticker: '{ticker}',")
    js_lines.append("    data: [")
    
    for j, date in enumerate(sorted_dates):
        if date in ticker_data[ticker]:
            rs = ticker_data[ticker][date]['rs']
            change = ticker_data[ticker][date]['change']
            date_display = date.replace('-', ' Thg ').replace('2026', '').replace('04', '04')
            # Format date properly
            parts = date.split('-')
            date_display = f"{parts[2]} Thg {parts[1]}"
            
            js_lines.append(f"      {{ date: '{date_display}', rs: {rs}, change: {change:.2f} }},")
    
    # Remove the last comma from last date entry
    if js_lines[-1].endswith(','):
        js_lines[-1] = js_lines[-1].rstrip(',')
    
    js_lines.append("    ]")
    
    if i < len(ranking) - 1:
        js_lines.append("  },")
    else:
        js_lines.append("  }")

js_lines.append("];")

js_code = "\n".join(js_lines)

# Write to file
with open('rs_heatmap_data.js', 'w', encoding='utf-8') as f:
    f.write(js_code)

print(f"\nGenerated rs_heatmap_data.js with {len(ranking)} tickers")
print("\nFirst 10 tickers in ranking:")
for i, row in enumerate(ranking[:10], 1):
    print(f"{i:3}. {row['ticker']:6} RS={row['rs']:3}")
