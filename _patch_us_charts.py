"""One-off patcher: refresh CBOE VIX (100 sessions) and add Nasdaq Composite (100 sessions)
chart into the already-generated market_breadth.html, without re-running the full breadth pipeline.

Idempotent: re-running replaces the existing VIX block and Nasdaq block.
"""
import json
import re
from pathlib import Path

import pandas as pd
import yfinance as yf

HERE = Path(__file__).parent
HTML_PATH = HERE / "market_breadth.html"
SESSIONS = 100


def fetch(symbol: str, label: str) -> pd.DataFrame:
    df = yf.download(symbol, period="1y", interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"{label} ({symbol}) returned no rows")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower().replace(" ", "_") for c in df.columns.to_flat_index()]
    else:
        df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    df = df.reset_index().rename(columns={"Date": "time", "date": "time"})
    if "volume" not in df.columns:
        df["volume"] = 0
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").tail(SESSIONS).reset_index(drop=True)
    return df


def candle_and_vol(df: pd.DataFrame, name: str, vol_divisor: float, vol_label: str):
    dates = [d.strftime("%d-%m-%Y") for d in df["time"]]
    candle = {
        "type": "candlestick",
        "x": dates,
        "open": df["open"].round(2).tolist(),
        "high": df["high"].round(2).tolist(),
        "low": df["low"].round(2).tolist(),
        "close": df["close"].round(2).tolist(),
        "name": name,
        "increasing": {"line": {"color": "#43A047"}, "fillcolor": "#43A047"},
        "decreasing": {"line": {"color": "#E53935"}, "fillcolor": "#E53935"},
    }
    colors = ["#43A047" if c >= o else "#E53935" for c, o in zip(df["close"], df["open"])]
    vol_y = (df["volume"].fillna(0) / vol_divisor).round(2).tolist() if vol_divisor != 1 else df["volume"].fillna(0).round(0).tolist()
    bar = {
        "type": "bar",
        "x": dates,
        "y": vol_y,
        "name": vol_label,
        "marker": {"color": colors, "opacity": 0.6},
        "xaxis": "x",
        "yaxis": "y2",
    }
    return json.dumps([candle]), json.dumps([bar])


def main() -> None:
    print("Fetching CBOE VIX ...")
    vix = fetch("^VIX", "CBOE VIX")
    print(f"  VIX rows: {len(vix)} | last close {vix['close'].iloc[-1]:.2f} on {vix['time'].iloc[-1].date()}")

    print("Fetching Nasdaq Composite ...")
    ndx = fetch("^IXIC", "Nasdaq Composite")
    print(f"  NDX rows: {len(ndx)} | last close {ndx['close'].iloc[-1]:.2f} on {ndx['time'].iloc[-1].date()}")

    vix_data, vix_vol = candle_and_vol(vix, "CBOE VIX", vol_divisor=1, vol_label="Volume")
    ndx_data, ndx_vol = candle_and_vol(ndx, "Nasdaq Composite", vol_divisor=1e9, vol_label="Volume (tỷ)")

    html = HTML_PATH.read_text(encoding="utf-8")

    # 1) Ensure the Nasdaq <div> exists right below the VIX <div>.
    if 'id="nasdaq-chart"' not in html:
        html = html.replace(
            '<div id="vix-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>',
            '<div id="vix-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>\n\n  <div id="nasdaq-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>',
            1,
        )

    # 2) Replace the VIX data + vol JS lines with fresh 100-session data.
    html = re.sub(
        r"const vixData = .*?;\nconst vixVol  = .*?;",
        f"const vixData = {vix_data};\nconst vixVol  = {vix_vol};",
        html,
        count=1,
        flags=re.DOTALL,
    )

    # 3) Update VIX title (50 → 100, fix mojibake).
    html = re.sub(
        r"title: \{ text: 'CBOE VIX - [^']+', font: \{ color: '#c0392b', size: 18 \} \}",
        "title: { text: 'CBOE VIX - 100 phiên', font: { color: '#c0392b', size: 18 } }",
        html,
        count=1,
    )

    # 4) Inject the Nasdaq Plotly block right after the VIX else-branch (idempotent).
    nasdaq_block = f"""

// Nasdaq Composite chart (100 sessions)
const ndxData = {ndx_data};
const ndxVol  = {ndx_vol};
if (ndxData && ndxVol) {{
  const ndxLayout = {{
    title: {{ text: 'Nasdaq Composite (^IXIC) - 100 phiên', font: {{ color: '#c0392b', size: 18 }} }},
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
      title: 'KL (tỷ)',
      showgrid: false,
      domain: [0, 0.25],
    }},
    legend: {{ orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center' }},
    hovermode: 'x unified',
    margin: {{ l: 60, r: 60, t: 60, b: 100 }},
    height: 624,
  }};
  Plotly.newPlot('nasdaq-chart', [...ndxData, ...ndxVol], ndxLayout, config);
}} else {{
  const ndxChart = document.getElementById('nasdaq-chart');
  if (ndxChart) {{
    ndxChart.style.display = 'none';
  }}
}}
"""

    if "Nasdaq Composite chart (100 sessions)" in html:
        # Replace existing block
        html = re.sub(
            r"\n*// Nasdaq Composite chart \(100 sessions\).*?(?=\nconst rsSearch)",
            nasdaq_block.strip("\n") + "\n\n",
            html,
            count=1,
            flags=re.DOTALL,
        )
    else:
        # Insert after the VIX else closing brace, before the rsSearch block
        anchor = "const rsSearch = document.getElementById('rs-search');"
        if anchor not in html:
            raise SystemExit("Could not find rsSearch anchor in HTML")
        html = html.replace(anchor, nasdaq_block.lstrip("\n") + "\n" + anchor, 1)

    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"Patched {HTML_PATH}")


if __name__ == "__main__":
    main()
