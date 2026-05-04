"""Pre-breakout signal engine.

Two layers of "stocks about to jump" signals, computed daily.

Both layers gate on the **composite RS Rating** from rs_matrix_3T.csv
(blend: 30% relative-performance percentile + 70% weighted-momentum percentile,
scaled to 1-99). The trigger threshold is 90 ("elite", top ~10%); watch list
relaxes to 80 ("leading", top ~20%).

Layer A — Composite RS leader still in base
  Signal: rs_rating ≥ 90 AND price ≤ 95% of trailing 252-day high.
  Reading: relative-strength leader; price has not broken out yet.

Layer B — Composite RS leader with Bollinger squeeze
  BB(20, 2σ) on close,  BB_width = (upper - lower) / middle
  Squeeze = today's BB_width is in the bottom 20% of its trailing 126-session distribution.
  Signal: rs_rating ≥ 90 AND in squeeze.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# --- Tunables ---------------------------------------------------------------
WINDOW_52W            = 252      # trading days
PRICE_BASE_MAX        = 0.95     # price ≤ 95% of rolling max → still in base
RS_RATING_TRIGGER     = 90       # composite rs_rating threshold for "Triggered"
RS_RATING_WATCH       = 80       # composite rs_rating threshold for "Watch"
BB_PERIOD             = 20
BB_K                  = 2.0
BB_PCTILE_HIST        = 126      # distribution window for squeeze percentile
SQUEEZE_PCTILE        = 20.0     # bottom 20% = "in squeeze" (trigger)
SQUEEZE_PCTILE_WATCH  = 40.0     # bottom 40% = "near squeeze" (watch)


@dataclass
class PreBreakoutResult:
    layer_a: list[dict]      # triggered (strict)
    layer_a_watch: list[dict] # near-trigger watch list
    layer_b: list[dict]      # triggered (strict)
    layer_b_watch: list[dict] # near-trigger watch list
    both:    list[dict]      # tickers passing both layers
    meta:    dict


def _load_ohlc(combined_csv: Path) -> dict[str, pd.DataFrame]:
    """Returns ticker → DataFrame indexed by date with 'close' column, sorted ascending."""
    raw = pd.read_csv(combined_csv, encoding="utf-8-sig")
    raw["time"] = pd.to_datetime(raw["time"], errors="coerce")
    raw = raw.dropna(subset=["time", "close"]).sort_values(["ticker", "time"])
    out: dict[str, pd.DataFrame] = {}
    for tkr, g in raw.groupby("ticker"):
        g = g.drop_duplicates("time", keep="last").set_index("time")
        out[str(tkr)] = g[["close"]].astype(float)
    return out


def _load_latest_rs_ratings(rs_matrix_csv: Path) -> dict[str, int]:
    """Map ticker → composite rs_rating from the latest session in rs_matrix_3T.csv."""
    df = pd.read_csv(rs_matrix_csv, encoding="utf-8-sig")
    if df.empty or "rs_rating" not in df.columns or "session_date" not in df.columns:
        return {}
    df["session_date"] = pd.to_datetime(df["session_date"], errors="coerce")
    latest = df.loc[df["session_date"] == df["session_date"].max()].copy()
    latest["ticker"] = latest["ticker"].astype(str).str.strip().str.upper()
    latest["rs_rating"] = pd.to_numeric(latest["rs_rating"], errors="coerce")
    latest = latest.dropna(subset=["ticker", "rs_rating"])
    return {row.ticker: int(row.rs_rating) for row in latest.itertuples(index=False)}


def _bb_width(close: pd.Series, period: int = BB_PERIOD, k: float = BB_K) -> pd.Series:
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper = ma + k * sd
    lower = ma - k * sd
    return (upper - lower) / ma  # normalized width


def compute(
    combined_csv: Path,
    rs_universe_csv: Path,  # accepts the unified universe (rs_fixed_tickers.csv)
    rs_matrix_csv: Path | None = None,
    vnindex_ticker: str = "VNINDEX",
) -> PreBreakoutResult:
    bars = _load_ohlc(combined_csv)
    if vnindex_ticker not in bars:
        raise RuntimeError(f"{vnindex_ticker} not found in {combined_csv}")

    # Composite RS rating lookup (gates both layers)
    if rs_matrix_csv is None:
        rs_matrix_csv = Path(__file__).parent / "rs_matrix_3T.csv"
    rs_ratings = _load_latest_rs_ratings(rs_matrix_csv) if rs_matrix_csv.exists() else {}

    rs_uni = pd.read_csv(rs_universe_csv, encoding="utf-8-sig")
    universe = sorted(set(rs_uni["ticker"].astype(str).str.strip().str.upper()))
    available = [t for t in universe if t in bars and t != vnindex_ticker]
    missing_ohlc = [t for t in universe if t not in bars]
    missing_rating = [t for t in available if t not in rs_ratings]
    rated = [t for t in available if t in rs_ratings]

    layer_a: list[dict] = []
    layer_a_watch_pool: list[dict] = []
    layer_b: list[dict] = []
    layer_b_watch_pool: list[dict] = []

    for tkr in rated:
        rs_rating = rs_ratings[tkr]
        s_close = bars[tkr]["close"]
        if len(s_close) < 60:
            continue

        # --- Layer A: composite RS leader still in base ---------------------
        px_max_series = s_close.rolling(WINDOW_52W, min_periods=60).max()
        px_now = float(s_close.iloc[-1])
        px_peak = float(px_max_series.iloc[-1]) if pd.notna(px_max_series.iloc[-1]) else None
        if px_peak and px_peak > 0:
            px_pct = (px_now / px_peak - 1) * 100  # 0 = at peak, negative = below
            in_base = px_now < PRICE_BASE_MAX * px_peak
            row_a = {
                "ticker": tkr,
                "close": round(px_now, 2),
                "rs_rating": rs_rating,
                "pct_below_52w_high": round(px_pct, 2),
                "window_days": int(min(len(s_close), WINDOW_52W)),
            }
            if rs_rating >= RS_RATING_TRIGGER and in_base:
                layer_a.append(row_a)
            elif rs_rating >= RS_RATING_WATCH and in_base:
                layer_a_watch_pool.append(row_a)

        # --- Layer B: composite RS leader with BB squeeze -------------------
        bb_w = _bb_width(s_close)
        bb_now = bb_w.iloc[-1]
        bb_hist = bb_w.iloc[-BB_PCTILE_HIST:].dropna()
        if pd.notna(bb_now) and len(bb_hist) >= 20:
            pct = float((bb_hist < bb_now).mean() * 100)
            row_b = {
                "ticker": tkr,
                "close": round(px_now, 2),
                "rs_rating": rs_rating,
                "bb_width_pct": round(float(bb_now) * 100, 2),
                "bb_width_percentile": round(pct, 1),
            }
            if rs_rating >= RS_RATING_TRIGGER and pct <= SQUEEZE_PCTILE:
                layer_b.append(row_b)
            elif rs_rating >= RS_RATING_WATCH and pct <= SQUEEZE_PCTILE_WATCH:
                layer_b_watch_pool.append(row_b)

    # Triggered: sorted by rs_rating desc; tiebreak by closest-to-trigger metric
    layer_a.sort(key=lambda r: (r["rs_rating"], r["pct_below_52w_high"]), reverse=True)
    layer_b.sort(key=lambda r: (r["rs_rating"], -r["bb_width_percentile"]), reverse=True)
    # Watch lists: top 10 closest to triggering
    layer_a_watch_pool.sort(key=lambda r: (r["rs_rating"], r["pct_below_52w_high"]), reverse=True)
    layer_b_watch_pool.sort(key=lambda r: (r["rs_rating"], -r["bb_width_percentile"]), reverse=True)
    layer_a_watch = layer_a_watch_pool[:10]
    layer_b_watch = layer_b_watch_pool[:10]

    a_set = {r["ticker"] for r in layer_a}
    b_set = {r["ticker"] for r in layer_b}
    both_tickers = sorted(a_set & b_set)
    a_by_t = {r["ticker"]: r for r in layer_a}
    b_by_t = {r["ticker"]: r for r in layer_b}
    both = [{"ticker": t, "a": a_by_t[t], "b": b_by_t[t]} for t in both_tickers]

    meta = {
        "universe_count": len(universe),
        "analyzed_count": len(rated),
        "missing_count":  len(missing_ohlc) + len(missing_rating),
        "missing_ohlc_count":   len(missing_ohlc),
        "missing_rating_count": len(missing_rating),
        "missing_sample": (missing_ohlc + missing_rating)[:15],
        "rs_matrix_csv": str(rs_matrix_csv),
        "params": {
            "window_52w": WINDOW_52W,
            "price_base_max": PRICE_BASE_MAX,
            "rs_rating_trigger": RS_RATING_TRIGGER,
            "rs_rating_watch": RS_RATING_WATCH,
            "bb_period": BB_PERIOD,
            "bb_k": BB_K,
            "squeeze_percentile": SQUEEZE_PCTILE,
            "squeeze_percentile_watch": SQUEEZE_PCTILE_WATCH,
        },
    }
    return PreBreakoutResult(
        layer_a=layer_a,
        layer_a_watch=layer_a_watch,
        layer_b=layer_b,
        layer_b_watch=layer_b_watch,
        both=both,
        meta=meta,
    )


if __name__ == "__main__":
    import sys
    here = Path(__file__).parent
    candidates = sorted(here.glob("data/*/combined_dataset.csv"))
    if not candidates:
        print("No combined_dataset.csv found under data/<date>/", file=sys.stderr)
        sys.exit(1)
    latest = candidates[-1]
    result = compute(latest, here / "rs_fixed_tickers.csv", here / "rs_matrix_3T.csv")
    print(json.dumps({
        "meta": result.meta,
        "both": result.both,
        "layer_a": result.layer_a,
        "layer_a_watch": result.layer_a_watch,
        "layer_b": result.layer_b,
        "layer_b_watch": result.layer_b_watch,
    }, ensure_ascii=False, indent=2))
