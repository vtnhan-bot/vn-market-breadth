"""Pre-breakout signal engine.

Two layers of "stocks about to jump" signals, computed daily from local OHLC:

Layer A — RS Line divergence (O'Neil classic)
  RS_Line[t] = stock_close[t] / vnindex_close[t]
  Signal:  RS_Line at trailing 252-day high (1% tolerance)
       AND price still ≤ 95% of trailing 252-day price high
  Reading: relative strength is leading; price is still in a base.

Layer B — Mansfield RS_Ratio + Bollinger Band squeeze
  RS_Ratio = (1 + stock_6mo_return) / (1 + vnindex_6mo_return)
  BB(20, 2σ) on close,  BB_width = (upper - lower) / middle
  Squeeze = today's BB_width is in the bottom 20% of its trailing 126-session distribution
  Signal:  RS_Ratio > 1.20  AND  in squeeze.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# --- Tunables ---------------------------------------------------------------
WINDOW_52W      = 252      # trading days
RS_HIGH_TOL     = 0.99     # RS at-high if ≥ 99% of rolling max
PRICE_BASE_MAX  = 0.95     # price ≤ 95% of rolling max → still in base
RETURN_LOOKBACK = 126      # ~6 months of trading days
RS_RATIO_THRESH = 1.20     # > 1.20 = beat index by 20% over lookback
BB_PERIOD       = 20
BB_K            = 2.0
BB_PCTILE_HIST  = 126      # distribution window for squeeze percentile
SQUEEZE_PCTILE  = 20.0     # bottom 20% = "in squeeze"


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


def _bb_width(close: pd.Series, period: int = BB_PERIOD, k: float = BB_K) -> pd.Series:
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper = ma + k * sd
    lower = ma - k * sd
    return (upper - lower) / ma  # normalized width


def compute(
    combined_csv: Path,
    rs_universe_csv: Path,
    vnindex_ticker: str = "VNINDEX",
) -> PreBreakoutResult:
    bars = _load_ohlc(combined_csv)
    if vnindex_ticker not in bars:
        raise RuntimeError(f"{vnindex_ticker} not found in {combined_csv}")
    vni_close = bars[vnindex_ticker]["close"]

    rs_uni = pd.read_csv(rs_universe_csv, encoding="utf-8-sig")
    universe = sorted(set(rs_uni["ticker"].astype(str).str.strip().str.upper()))
    available = [t for t in universe if t in bars and t != vnindex_ticker]
    missing = [t for t in universe if t not in bars]

    layer_a: list[dict] = []
    layer_a_watch_pool: list[dict] = []
    layer_b: list[dict] = []
    layer_b_watch_pool: list[dict] = []

    for tkr in available:
        s_close = bars[tkr]["close"]
        # Align to common dates with VNINDEX
        df = pd.concat([s_close.rename("s"), vni_close.rename("v")], axis=1, join="inner").dropna()
        if len(df) < 60:
            continue

        # --- Layer A: RS-line divergence ------------------------------------
        rs_line = df["s"] / df["v"]
        # Use min_periods=60 so newer listings still get a partial window
        rs_max = rs_line.rolling(WINDOW_52W, min_periods=60).max()
        px_max = df["s"].rolling(WINDOW_52W, min_periods=60).max()

        rs_now, rs_peak = rs_line.iloc[-1], rs_max.iloc[-1]
        px_now, px_peak = df["s"].iloc[-1], px_max.iloc[-1]
        if pd.notna(rs_peak) and pd.notna(px_peak) and rs_peak > 0 and px_peak > 0:
            rs_pct = float(rs_now / rs_peak - 1) * 100   # 0 = at peak, negative = below
            px_pct = float(px_now / px_peak - 1) * 100
            row = {
                "ticker": tkr,
                "close": round(float(px_now), 2),
                "pct_below_52w_high": round(px_pct, 2),
                "rs_vs_peak_pct": round(rs_pct, 2),
                "window_days": int(min(len(df), WINDOW_52W)),
            }
            rs_at_high = rs_now >= RS_HIGH_TOL * rs_peak
            in_base    = px_now < PRICE_BASE_MAX * px_peak
            if rs_at_high and in_base:
                layer_a.append(row)
            elif in_base:
                # Watch pool: any in-base ticker; we'll rank and take top 10 below
                layer_a_watch_pool.append(row)

        # --- Layer B: Mansfield RS_Ratio + BB squeeze -----------------------
        if len(df) > RETURN_LOOKBACK:
            s_then = df["s"].iloc[-(RETURN_LOOKBACK + 1)]
            v_then = df["v"].iloc[-(RETURN_LOOKBACK + 1)]
            if s_then > 0 and v_then > 0:
                stock_ret = px_now / s_then - 1
                vni_ret   = df["v"].iloc[-1] / v_then - 1
                rs_ratio  = (1 + stock_ret) / (1 + vni_ret)
            else:
                rs_ratio = float("nan")
        else:
            rs_ratio = float("nan")

        bb_w = _bb_width(df["s"])
        bb_now = bb_w.iloc[-1]
        bb_hist = bb_w.iloc[-BB_PCTILE_HIST:].dropna()
        if pd.notna(bb_now) and len(bb_hist) >= 20:
            pct = float((bb_hist < bb_now).mean() * 100)
        else:
            pct = float("nan")

        if pd.notna(rs_ratio) and pd.notna(pct):
            row_b = {
                "ticker": tkr,
                "close": round(float(px_now), 2),
                "rs_ratio": round(float(rs_ratio), 3),
                "stock_ret_6mo_pct": round(float(stock_ret) * 100, 2),
                "vni_ret_6mo_pct": round(float(vni_ret) * 100, 2),
                "bb_width_pct": round(float(bb_now) * 100, 2),
                "bb_width_percentile": round(pct, 1),
            }
            if rs_ratio > RS_RATIO_THRESH and pct <= SQUEEZE_PCTILE:
                layer_b.append(row_b)
            elif rs_ratio > 1.0 and pct <= 40.0:
                layer_b_watch_pool.append(row_b)

    layer_a.sort(key=lambda r: r["rs_vs_peak_pct"], reverse=True)
    layer_b.sort(key=lambda r: (r["rs_ratio"], -r["bb_width_percentile"]), reverse=True)
    # Watch lists: top 10, ranked closest to triggering
    layer_a_watch_pool.sort(key=lambda r: r["rs_vs_peak_pct"], reverse=True)
    layer_b_watch_pool.sort(key=lambda r: (r["rs_ratio"], -r["bb_width_percentile"]), reverse=True)
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
        "analyzed_count": len(available),
        "missing_count":  len(missing),
        "missing_sample": missing[:15],
        "params": {
            "window_52w": WINDOW_52W,
            "rs_high_tolerance": RS_HIGH_TOL,
            "price_base_max": PRICE_BASE_MAX,
            "return_lookback_days": RETURN_LOOKBACK,
            "rs_ratio_threshold": RS_RATIO_THRESH,
            "bb_period": BB_PERIOD,
            "bb_k": BB_K,
            "squeeze_percentile": SQUEEZE_PCTILE,
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
    # Find the latest dated combined_dataset.csv
    candidates = sorted(here.glob("data/*/combined_dataset.csv"))
    if not candidates:
        print("No combined_dataset.csv found under data/<date>/", file=sys.stderr)
        sys.exit(1)
    latest = candidates[-1]
    result = compute(latest, here / "rs_universe.csv")
    print(json.dumps({
        "meta": result.meta,
        "both": result.both,
        "layer_a": result.layer_a,
        "layer_a_watch": result.layer_a_watch,
        "layer_b": result.layer_b,
        "layer_b_watch": result.layer_b_watch,
    }, ensure_ascii=False, indent=2))
