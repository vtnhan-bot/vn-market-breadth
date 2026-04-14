#!/usr/bin/env python3
"""
Vietnam Market Breadth - "Cơ hội" Chart Generator
Formula: mbzN = % of top-100 stocks (by market cap, HOSE+HNX) with Close > N-day SMA
Periods: 3, 5, 10, 20, 50, 200 sessions
"""

import os
import sys
import json
import time
import webbrowser
import warnings
import pickle
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_PATH = r"C:\Users\DELL\Desktop\vietnam_top100_marketcap_hose_hnx_best_effort.xlsx"
SCRIPT_DIR = Path(__file__).parent
CACHE_DIR  = SCRIPT_DIR / "cache"
OUTPUT_HTML = SCRIPT_DIR / "market_breadth.html"

MA_PERIODS      = [3, 5, 10, 20, 50, 200]
SESSIONS_SHOW   = 50
FETCH_DAYS_BACK = 420   # Need 200-day SMA + 50 sessions buffer + weekends
IS_CI           = bool(os.environ.get("GITHUB_ACTIONS"))
MAX_WORKERS     = 1 if IS_CI else 3
CACHE_HOURS     = 4     # Re-fetch if cache older than N hours
REQUEST_DELAY   = 3.5 if IS_CI else 0.3   # VCI guest limit: 20 req/min

MA_COLORS = {
    3:   "#00BCD4",   # cyan
    5:   "#FFA726",   # orange
    10:  "#43A047",   # green
    20:  "#9C27B0",   # purple
    50:  "#000000",   # black
    200: "#E53935",   # red
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"[{ts}] {msg.encode('ascii', 'replace').decode('ascii')}", flush=True)

def ma_key(p):
    return f"mbz{p:02d}" if p < 100 else f"mbz{p}"

# ─── STEP 1: Read tickers (Excel if available, else CSV fallback) ─────────────
def read_tickers():
    csv_path = SCRIPT_DIR / "tickers.csv"
    try:
        df = pd.read_excel(EXCEL_PATH, header=2)
        tickers = df["Ticker"].dropna().astype(str).str.strip().tolist()
        tickers = [t for t in tickers if t and t != "nan"][:100]
        log(f"Loaded {len(tickers)} tickers from Excel")
        return tickers
    except Exception:
        pass
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        tickers = df["Ticker"].dropna().astype(str).str.strip().tolist()
        tickers = [t for t in tickers if t and t != "nan"][:100]
        log(f"Loaded {len(tickers)} tickers from tickers.csv (Excel not found)")
        return tickers
    raise FileNotFoundError("No ticker source found. Provide Excel or tickers.csv.")

# ─── STEP 2: Fetch price data with caching ───────────────────────────────────
CACHE_DIR.mkdir(exist_ok=True)

def cache_path(ticker):
    return CACHE_DIR / f"{ticker}.pkl"

def is_cache_fresh(ticker):
    p = cache_path(ticker)
    if not p.exists():
        return False
    age_hours = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    return age_hours < CACHE_HOURS

def load_cache(ticker):
    try:
        with open(cache_path(ticker), "rb") as f:
            return pickle.load(f)
    except Exception:
        return None

def save_cache(ticker, df):
    with open(cache_path(ticker), "wb") as f:
        pickle.dump(df, f)

def fetch_one(ticker, start_date, end_date):
    if is_cache_fresh(ticker):
        df = load_cache(ticker)
        if df is not None and len(df) > 0:
            return ticker, df, "cache"

    time.sleep(REQUEST_DELAY)
    try:
        from vnstock import Vnstock
        for source in ["VCI", "TCBS"]:
            try:
                s = Vnstock().stock(symbol=ticker, source=source)
                df = s.quote.history(start=start_date, end=end_date, interval="1D")
                if df is not None and len(df) >= 10:
                    df = df[["time", "close"]].copy()
                    df["time"] = pd.to_datetime(df["time"]).dt.date
                    df = df.dropna().sort_values("time").reset_index(drop=True)
                    save_cache(ticker, df)
                    return ticker, df, source
                time.sleep(0.5)
            except Exception:
                time.sleep(1.0)
                continue
    except Exception as e:
        pass
    return ticker, None, "failed"

def fetch_all(tickers, start_date, end_date):
    price_data = {}
    failed = []
    total = len(tickers)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one, t, start_date, end_date): t for t in tickers}
        done = 0
        for fut in as_completed(futures):
            ticker, df, source = fut.result()
            done += 1
            if df is not None:
                price_data[ticker] = df
                status = f"OK ({source}, {len(df)} rows)"
            else:
                failed.append(ticker)
                status = "FAILED"
            sys.stdout.write(f"\r  Fetched {done}/{total} | {ticker}: {status}        ")
            sys.stdout.flush()

    print()
    if failed and not IS_CI:
        log(f"First pass failed ({len(failed)}): {', '.join(failed)}")
        log("Retrying failed tickers sequentially (may take ~1 min)...")
        still_failed = []
        for i, ticker in enumerate(failed):
            time.sleep(2.0)
            t, df, src = fetch_one(ticker, start_date, end_date)
            if df is not None:
                price_data[ticker] = df
                sys.stdout.write(f"\r  Retry {i+1}/{len(failed)} | {ticker}: OK ({src}, {len(df)} rows)        ")
            else:
                still_failed.append(ticker)
                sys.stdout.write(f"\r  Retry {i+1}/{len(failed)} | {ticker}: still failed        ")
            sys.stdout.flush()
        print()
        if still_failed:
            log(f"Permanently failed ({len(still_failed)}): {', '.join(still_failed)}")
    elif failed:
        log(f"Failed ({len(failed)}) in CI mode (no retry): {', '.join(failed)}")

    log(f"Successfully fetched data for {len(price_data)} tickers")
    return price_data

# ─── STEP 3: Calculate breadth ───────────────────────────────────────────────
def calculate_breadth(price_data, sessions_show=50):
    # Build wide close-price DataFrame: rows=dates, cols=tickers
    frames = []
    for ticker, df in price_data.items():
        tmp = df.set_index("time")["close"].rename(ticker)
        frames.append(tmp)

    prices = pd.concat(frames, axis=1).sort_index()
    prices.index = pd.to_datetime(prices.index)

    # Forward-fill single-day gaps (VN market sometimes has data gaps)
    prices = prices.ffill(limit=2)

    result_rows = []
    for p in MA_PERIODS:
        sma = prices.rolling(window=p, min_periods=p).mean()
        above = (prices > sma)
        n_above = above.sum(axis=1)
        n_total = sma.notna().sum(axis=1)   # only count tickers with enough history
        pct = (n_above / n_total.replace(0, np.nan) * 100).round(2)
        result_rows.append(pct.rename(ma_key(p)))

    breadth = pd.concat(result_rows, axis=1).dropna(how="all")
    breadth = breadth.tail(sessions_show)
    return breadth

# ─── STEP 4: Weekly analysis text ────────────────────────────────────────────
def generate_analysis(breadth, price_data):
    last = breadth.iloc[-1]
    prev = breadth.iloc[-2] if len(breadth) >= 2 else last
    week_ago = breadth.iloc[-6] if len(breadth) >= 6 else breadth.iloc[0]
    last_date = breadth.index[-1].strftime("%d/%m/%Y")

    def signal(v):
        if v is None or pd.isna(v): return "N/A", "gray"
        if v < 20:  return "Quá bán nặng", "#E53935"
        if v < 35:  return "Yếu", "#FF7043"
        if v < 50:  return "Trung tính thấp", "#FFA726"
        if v < 65:  return "Trung tính cao", "#66BB6A"
        if v < 80:  return "Mạnh", "#43A047"
        return "Quá mua", "#1E88E5"

    rows = []
    for p in MA_PERIODS:
        k = ma_key(p)
        v = last.get(k)
        pv = prev.get(k)
        wv = week_ago.get(k)
        sig, col = signal(v)
        delta_day  = round(v - pv, 2) if (v is not None and pv is not None and not pd.isna(v) and not pd.isna(pv)) else 0
        delta_week = round(v - wv, 2) if (v is not None and wv is not None and not pd.isna(v) and not pd.isna(wv)) else 0
        arrow_d = "▲" if delta_day > 0 else ("▼" if delta_day < 0 else "─")
        arrow_w = "▲" if delta_week > 0 else ("▼" if delta_week < 0 else "─")
        rows.append({
            "period": f"SMA-{p}", "key": k, "value": round(v, 2) if v and not pd.isna(v) else "N/A",
            "signal": sig, "color": col,
            "delta_day": f"{arrow_d} {abs(delta_day):.2f}%",
            "delta_week": f"{arrow_w} {abs(delta_week):.2f}%",
        })

    # Composite market score (weighted average)
    weights = {3: 0.05, 5: 0.10, 10: 0.15, 20: 0.20, 50: 0.25, 200: 0.25}
    total_w = 0
    score = 0
    for p in MA_PERIODS:
        k = ma_key(p)
        v = last.get(k)
        if v is not None and not pd.isna(v):
            score += v * weights[p]
            total_w += weights[p]
    composite = round(score / total_w, 1) if total_w > 0 else 0

    # Trend direction (5-session slope of mbz50)
    if len(breadth) >= 6:
        mbz50_recent = breadth[ma_key(50)].dropna().tail(6)
        slope50 = (mbz50_recent.iloc[-1] - mbz50_recent.iloc[0]) / max(len(mbz50_recent) - 1, 1)
    else:
        slope50 = 0

    # mbz03 momentum (fast indicator)
    mbz03_now = last.get(ma_key(3), 50)
    mbz50_now = last.get(ma_key(50), 0)
    mbz200_now = last.get(ma_key(200), 0)

    # Overall verdict
    if composite >= 60:
        verdict = "🟢 TÍCH CỰC — Thị trường rộng, đa số CP trên MA. Có thể tiếp tục nắm giữ/mua vào."
        verdict_color = "#43A047"
    elif composite >= 40:
        verdict = "🟡 TRUNG TÍNH — Thị trường phân hóa. Chọn lọc cổ phiếu mạnh, hạn chế mua đuổi."
        verdict_color = "#FFA726"
    elif composite >= 20:
        verdict = "🟠 THẬN TRỌNG — Phần lớn CP dưới MA. Theo dõi tín hiệu phục hồi trước khi vào hàng."
        verdict_color = "#FF7043"
    else:
        verdict = "🔴 TIÊU CỰC — Thị trường rất yếu. Ưu tiên phòng thủ, chờ mbz50 > 20% để xác nhận đáy."
        verdict_color = "#E53935"

    # Next-week outlook based on mbz03 momentum vs mbz50 trend
    if mbz03_now > 50 and slope50 > 0:
        next_week = "Tuần tới: Đà ngắn hạn đang phục hồi và mbz50 bắt đầu tăng → khả năng tiếp diễn tích cực."
    elif mbz03_now > 50 and slope50 <= 0:
        next_week = "Tuần tới: Ngắn hạn hồi phục nhưng xu hướng trung hạn (SMA-50) chưa xác nhận → cẩn thận bẫy hồi."
    elif mbz03_now <= 50 and slope50 > 0:
        next_week = "Tuần tới: Ngắn hạn chậm lại nhưng mbz50 đang cải thiện → theo dõi sát, có thể tích lũy từng bước."
    else:
        next_week = "Tuần tới: Cả ngắn hạn và trung hạn đều yếu → giữ tỷ trọng thấp, chờ xác nhận đảo chiều."

    return {
        "rows": rows,
        "composite": composite,
        "verdict": verdict,
        "verdict_color": verdict_color,
        "next_week": next_week,
        "last_date": last_date,
        "n_tickers": len(price_data),
    }

# ─── STEP 4b: Fetch VNIndex ──────────────────────────────────────────────────
def fetch_vnindex(start_date, end_date):
    cache_file = CACHE_DIR / "VNINDEX.pkl"
    age_hours = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600 if cache_file.exists() else 999
    if age_hours < CACHE_HOURS:
        try:
            with open(cache_file, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    try:
        from vnstock import Vnstock
        s = Vnstock().stock(symbol="VNINDEX", source="VCI")
        df = s.quote.history(start=start_date, end=end_date, interval="1D")
        df["time"] = pd.to_datetime(df["time"]).dt.date
        df = df.sort_values("time").reset_index(drop=True)
        with open(cache_file, "wb") as f:
            pickle.dump(df, f)
        return df
    except Exception as e:
        log(f"VNIndex fetch failed: {e}")
        return None

# ─── STEP 5: Build HTML ───────────────────────────────────────────────────────
def build_html(breadth, analysis, tickers, vnindex_df=None):
    dates = [d.strftime("%d-%m-%Y") for d in breadth.index]

    traces = []
    for p in MA_PERIODS:
        k = ma_key(p)
        vals = breadth[k].tolist() if k in breadth.columns else []
        vals_clean = [round(v, 2) if not pd.isna(v) else None for v in vals]
        line_style = {"color": MA_COLORS[p], "width": 4 if p == 50 else 2}
        if p in (3, 5):
            line_style["dash"] = "dot"
        traces.append({
            "x": dates, "y": vals_clean,
            "name": k, "mode": "lines+markers",
            "line": line_style,
            "marker": {"size": 4, "color": MA_COLORS[p]},
            "connectgaps": False,
        })

    chart_data = json.dumps(traces)

    # VNIndex chart data
    vni_chart_data = "null"
    vni_vol_data = "null"
    if vnindex_df is not None and len(vnindex_df) > 0:
        # Align to same date range as breadth
        vni = vnindex_df.copy()
        vni["time"] = pd.to_datetime(vni["time"])
        vni = vni[vni["time"] >= breadth.index[0]].tail(SESSIONS_SHOW)
        vni_dates = [d.strftime("%d-%m-%Y") for d in vni["time"]]
        candle = {
            "type": "candlestick",
            "x": vni_dates,
            "open":  vni["open"].round(2).tolist(),
            "high":  vni["high"].round(2).tolist(),
            "low":   vni["low"].round(2).tolist(),
            "close": vni["close"].round(2).tolist(),
            "name": "VNINDEX",
            "increasing": {"line": {"color": "#43A047"}, "fillcolor": "#43A047"},
            "decreasing": {"line": {"color": "#E53935"}, "fillcolor": "#E53935"},
        }
        colors = ["#43A047" if c >= o else "#E53935"
                  for c, o in zip(vni["close"], vni["open"])]
        vol_bar = {
            "type": "bar",
            "x": vni_dates,
            "y": (vni["volume"] / 1e6).round(1).tolist(),
            "name": "Volume (triệu)",
            "marker": {"color": colors, "opacity": 0.6},
            "xaxis": "x",
            "yaxis": "y2",
        }
        vni_chart_data = json.dumps([candle])
        vni_vol_data   = json.dumps([vol_bar])

    # Build analysis table rows HTML
    table_rows = ""
    for r in analysis["rows"]:
        table_rows += f"""
        <tr>
          <td style="font-weight:600;color:{MA_COLORS[int(r['key'].replace('mbz','') or 3)]}">{r['period']}</td>
          <td style="text-align:center;font-weight:700">{r['value']}%</td>
          <td style="text-align:center"><span style="color:{r['color']};font-weight:600">{r['signal']}</span></td>
          <td style="text-align:center">{r['delta_day']}</td>
          <td style="text-align:center">{r['delta_week']}</td>
        </tr>"""

    # Period color items for formula section
    formula_items = ""
    for p in MA_PERIODS:
        formula_items += f'<li><b style="color:{MA_COLORS[p]}">{ma_key(p)}</b> = (Số CP có Giá đóng cửa &gt; SMA-{p}) ÷ Tổng CP có đủ dữ liệu × 100</li>'

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Cơ hội - Độ rộng thị trường Việt Nam</title>
  <script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f5f0e8;
      color: #222;
      min-height: 100vh;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
    h1 {{
      text-align: center;
      color: #c0392b;
      font-size: 1.6rem;
      margin-bottom: 4px;
    }}
    .subtitle {{
      text-align: center;
      color: #777;
      font-size: 0.85rem;
      margin-bottom: 16px;
    }}
    #chart {{
      background: #fff8f0;
      border: 1px solid #e0d8cc;
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 24px;
    }}
    .panel {{
      background: #fff;
      border: 1px solid #e0d0c0;
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 20px;
    }}
    .panel h2 {{
      font-size: 1.1rem;
      margin-bottom: 14px;
      padding-bottom: 8px;
      border-bottom: 2px solid #f0e8dc;
      color: #333;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th {{
      background: #f0e8dc;
      padding: 8px 12px;
      text-align: left;
      font-weight: 600;
      color: #444;
    }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #f5f0ea; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fdf8f4; }}
    .verdict-box {{
      padding: 14px 18px;
      border-radius: 6px;
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 12px;
      border-left: 5px solid {analysis['verdict_color']};
      background: #fafafa;
      color: {analysis['verdict_color']};
    }}
    .next-week-box {{
      padding: 12px 16px;
      background: #f0f8ff;
      border-left: 4px solid #1E88E5;
      border-radius: 4px;
      color: #1565C0;
      font-size: 0.95rem;
    }}
    .composite {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .gauge {{
      width: 80px; height: 80px;
      border-radius: 50%;
      background: conic-gradient(
        {analysis['verdict_color']} {analysis['composite'] * 3.6}deg,
        #e0e0e0 {analysis['composite'] * 3.6}deg
      );
      display: flex; align-items: center; justify-content: center;
      font-size: 1.3rem;
      font-weight: 700;
      color: #222;
      position: relative;
    }}
    .gauge::before {{
      content: '';
      position: absolute;
      width: 58px; height: 58px;
      background: #fff;
      border-radius: 50%;
    }}
    .gauge span {{ position: relative; z-index: 1; font-size: 1rem; }}
    .formula-list {{ font-size: 0.88rem; line-height: 2; }}
    .formula-list li {{ list-style: none; padding: 2px 0; }}
    .note {{ font-size: 0.8rem; color: #888; margin-top: 10px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    @media (max-width: 700px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
    .tag {{
      display: inline-block;
      background: #f0e8dc;
      border-radius: 4px;
      padding: 2px 8px;
      font-size: 0.8rem;
      color: #666;
      margin-left: 6px;
    }}
  </style>
</head>
<body>
<div class="container">
  <h1>📊 Cơ hội - Độ rộng thị trường (50 phiên)</h1>
  <div class="subtitle">Top 100 cổ phiếu vốn hóa lớn HOSE + HNX &nbsp;|&nbsp; Cập nhật: {now_str} &nbsp;|&nbsp; {analysis['n_tickers']} CP có dữ liệu</div>

  <div id="chart"></div>

  <div id="vnindex-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>

  <div class="panel">
    <h2>🧮 Nhận định tuần tới <span class="tag">{analysis['last_date']}</span></h2>
    <div class="composite">
      <div class="gauge"><span>{analysis['composite']}</span></div>
      <div>
        <div style="font-size:0.8rem;color:#888;margin-bottom:4px">Điểm tổng hợp (0–100)</div>
        <div class="verdict-box">{analysis['verdict']}</div>
      </div>
    </div>
    <div class="next-week-box">{analysis['next_week']}</div>
  </div>

  <div class="grid-2">
    <div class="panel">
      <h2>📋 Chi tiết chỉ số</h2>
      <table>
        <thead>
          <tr>
            <th>Chỉ số</th>
            <th style="text-align:center">Hiện tại</th>
            <th style="text-align:center">Trạng thái</th>
            <th style="text-align:center">Δ ngày</th>
            <th style="text-align:center">Δ tuần</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      <p class="note">▲/▼ = thay đổi so với phiên trước / 5 phiên trước</p>
    </div>

    <div class="panel">
      <h2>📐 Công thức tính</h2>
      <p style="font-size:0.88rem;margin-bottom:10px;color:#555">
        <b>mbzN</b> = (Số cổ phiếu trong top-100 có <em>Giá đóng cửa &gt; SMA(N)</em>) ÷ Tổng cổ phiếu có đủ dữ liệu × 100
      </p>
      <ul class="formula-list">
        {formula_items}
      </ul>
      <p class="note" style="margin-top:14px">
        <b>Cách đọc:</b><br>
        &gt; 80%: Quá mua 🔵 &nbsp;|&nbsp; 60–80%: Mạnh 🟢 &nbsp;|&nbsp; 40–60%: Trung tính 🟡<br>
        20–40%: Yếu 🟠 &nbsp;|&nbsp; &lt; 20%: Quá bán 🔴 (cơ hội mua)
      </p>
    </div>
  </div>


  <div class="panel">
    <h2>📚 Giải thích mbz200 &gt; mbz50 hiện tại</h2>
    <p style="font-size:0.88rem;color:#555;line-height:1.7">
      Khi <b>mbz50 &lt; mbz200</b>, thị trường đang trong giai đoạn <em>điều chỉnh trung hạn nhưng xu hướng dài hạn còn tích cực</em>.
      Nghĩa là nhiều CP đã phá vỡ SMA-50 (bán tháo gần đây) nhưng vẫn duy trì được trên SMA-200 (nền tảng dài hạn).
      Đây thường là <b>vùng tích lũy tốt</b> cho nhà đầu tư dài hạn, nhưng cần chờ mbz50 hồi phục &gt;20% để xác nhận đà phục hồi bền vững.
    </p>
  </div>

  <p class="note" style="text-align:center;padding-bottom:20px">
    Dữ liệu: vnstock (VCI/TCBS) &nbsp;|&nbsp; Vũ trụ: {analysis['n_tickers']} CP top-100 HOSE+HNX &nbsp;|&nbsp;
    Chạy lại script để cập nhật &nbsp;|&nbsp; Cache: {CACHE_HOURS}h
  </p>
</div>

<script>
const traces = {chart_data};
const layout = {{
  title: {{ text: 'Cơ hội - 50 phiên', font: {{ color: '#c0392b', size: 18 }} }},
  paper_bgcolor: '#fff8f0',
  plot_bgcolor: '#fff8f0',
  xaxis: {{
    type: 'category',
    tickangle: -45,
    tickfont: {{ size: 10 }},
    gridcolor: '#ead8c0',
    showgrid: true,
  }},
  yaxis: {{
    range: [0, 105],
    ticksuffix: '%',
    gridcolor: '#ead8c0',
    showgrid: true,
  }},
  legend: {{ orientation: 'h', y: -0.25, x: 0.5, xanchor: 'center' }},
  hovermode: 'x unified',
  margin: {{ l: 50, r: 80, t: 60, b: 120 }},
  height: 624,
}};
const config = {{ responsive: true, displayModeBar: true }};
Plotly.newPlot('chart', traces, layout, config);

// VNIndex chart
const vniData = {vni_chart_data};
const vniVol  = {vni_vol_data};
if (vniData && vniVol) {{
  const vniLayout = {{
    title: {{ text: 'VN-Index - 50 phiên', font: {{ color: '#c0392b', size: 18 }} }},
    paper_bgcolor: '#fff8f0',
    plot_bgcolor: '#fff8f0',
    xaxis: {{
      type: 'category',
      tickangle: -45,
      tickfont: {{ size: 10 }},
      gridcolor: '#ead8c0',
      rangeslider: {{ visible: false }},
      anchor: 'y2',
      domain: [0, 1],
    }},
    yaxis: {{
      title: 'Điểm',
      gridcolor: '#ead8c0',
      domain: [0.30, 1],
    }},
    yaxis2: {{
      title: 'KL (triệu)',
      showgrid: false,
      domain: [0, 0.25],
    }},
    legend: {{ orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center' }},
    hovermode: 'x unified',
    margin: {{ l: 60, r: 60, t: 60, b: 100 }},
    height: 624,
  }};
  Plotly.newPlot('vnindex-chart', [...vniData, ...vniVol], vniLayout, config);
}}
</script>
</body>
</html>
"""
    return html

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true", help="Skip opening browser (for CI)")
    args = parser.parse_args()

    log("=== Vietnam Market Breadth Generator ===")

    # Step 1
    tickers = read_tickers()

    # Step 2
    end_date   = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=FETCH_DAYS_BACK)).strftime("%Y-%m-%d")
    log(f"Fetching data from {start_date} to {end_date} ...")
    price_data = fetch_all(tickers, start_date, end_date)

    if len(price_data) < 10:
        log("ERROR: Too few tickers fetched. Check internet / vnstock installation.")
        sys.exit(1)

    # Step 3
    log("Calculating breadth indicators ...")
    breadth = calculate_breadth(price_data, SESSIONS_SHOW)
    log(f"Breadth matrix: {breadth.shape[0]} sessions × {breadth.shape[1]} indicators")

    # Step 4
    log("Generating analysis ...")
    analysis = generate_analysis(breadth, price_data)
    log(f"Composite score: {analysis['composite']} | {analysis['verdict']}")

    # Step 4b
    log("Fetching VNIndex ...")
    vnindex_df = fetch_vnindex(start_date, end_date)
    if vnindex_df is not None:
        log(f"VNIndex: {len(vnindex_df)} rows")

    # Step 5
    log("Building HTML ...")
    html = build_html(breadth, analysis, tickers, vnindex_df)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Saved: {OUTPUT_HTML}")

    # Open browser (skip in CI)
    if not args.no_browser and not os.environ.get("GITHUB_ACTIONS"):
        webbrowser.open(OUTPUT_HTML.as_uri())
        log("Done! Browser should open automatically.")
    else:
        log("Done! (browser skipped in CI mode)")

if __name__ == "__main__":
    main()
