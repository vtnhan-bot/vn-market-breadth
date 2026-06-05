#!/usr/bin/env python3
"""Intraday Relative Strength snapshot for the 230-ticker RS universe.

Called from intraday_breadth.py's main loop (every 15 min during VN trading
hours). Reuses the EOD history that intraday_breadth already downloaded from
gs://vn-market-breadth/intraday/combined_dataset.csv, augments each ticker
with TODAY's intraday close (vnstock Trading.price_board), and produces a
fresh RS Rating snapshot.

Math note: we deliberately skip computing VNINDEX's 90-day return. In the
EOD pipeline, relative_performance = stock_return_90d - index_return_90d
is fed into a cross-section rank. The index return is the same constant
for every row, so subtracting it is identity for the percentile rank.
For the intraday update we rank stock_return_90d directly (and weighted
momentum directly) — produces the same rs_rating as the EOD path with
one fewer data dependency.

Output: gs://vn-market-breadth/intraday_rs_3T.json
  {
    "session_date": "2026-05-18",
    "tick_time_ict": "10:00",
    "last_updated_ict": "10:00 18/05/2026",
    "rows": [
      {"ticker":"VNM","rs_rating":87,"daily_change_pct":1.23,"intraday_price":61.5},
      ...
    ]
  }

The dashboard JS fetches this and prepends a new leftmost column to the RS
table during market hours. After 15:15 ICT, the daily pipeline overwrites
the whole heatmap with settled rs_matrix_3T.csv values.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
RS_UNIVERSE_PATH = SCRIPT_DIR / "rs_fixed_tickers.csv"
ICT = ZoneInfo("Asia/Ho_Chi_Minh")

GCS_BUCKET = os.environ.get("INTRADAY_GCS_BUCKET", "vn-market-breadth")
GCS_INTRADAY_RS_KEY = "intraday_rs_3T.json"

RS_LOOKBACK_CALENDAR_DAYS = 90  # match rs_matrix_3T.py
PRICE_DIVISOR = 1000.0           # vnstock raw VND -> combined_dataset's thousand VND

LOGGER = logging.getLogger("intraday_rs_3T")


def _load_rs_universe() -> list[str]:
    df = pd.read_csv(RS_UNIVERSE_PATH, encoding="utf-8-sig")
    tickers = [str(t).strip().upper() for t in df["ticker"].tolist() if pd.notna(t)]
    return [t for t in tickers if t and t.lower() != "nan"]


def _load_history_frame(combined_path: Path, tickers: list[str]) -> pd.DataFrame:
    """Load EOD history for the RS universe from combined_dataset.csv.

    Returns long-format frame with columns [ticker, time (date), close]. Only
    rows whose ticker is in the universe are kept.
    """
    df = pd.read_csv(combined_path, encoding="utf-8-sig")
    df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.date
    df = df.dropna(subset=["time", "ticker", "close"])
    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    universe_set = {t.upper() for t in tickers}
    df = df[df["ticker"].isin(universe_set)]
    return df[["ticker", "time", "close"]].sort_values(["ticker", "time"]).reset_index(drop=True)


def _fetch_intraday_prices(tickers: list[str]) -> dict[str, dict[str, float]]:
    """Single batch Trading.price_board() call. Returns ticker -> dict with
    match_price (intraday in 'thousand VND') and ref_price (yesterday's close
    reference, for daily_change_pct).
    """
    from vnstock import Trading
    trading = Trading(source="VCI")
    board = trading.price_board(tickers)
    if board is None or board.empty:
        raise RuntimeError("Trading.price_board() returned no rows for RS universe")

    symbols = board[("listing", "symbol")].astype(str).str.upper().str.strip()
    match_px = pd.to_numeric(board[("match", "match_price")], errors="coerce")
    ref_px = pd.to_numeric(board[("listing", "ref_price")], errors="coerce")

    out: dict[str, dict[str, float]] = {}
    for sym, m, r in zip(symbols, match_px, ref_px):
        price = m if pd.notna(m) and m > 0 else r
        if pd.notna(price) and price > 0:
            out[sym] = {
                "intraday_price": float(price) / PRICE_DIVISOR,
                "ref_price": float(r) / PRICE_DIVISOR if pd.notna(r) and r > 0 else float("nan"),
            }
    return out


def _compute_return_90d(history: pd.DataFrame, intraday_price: float, today: "datetime.date") -> float:
    """Stock's 90-calendar-day return: intraday_price / close_90d_ago - 1.

    Uses the most recent EOD bar on or before (today - 90 days) as the base.
    Matches rs_matrix_3T.calculate_return_90d semantics.
    """
    if intraday_price is None or pd.isna(intraday_price) or intraday_price <= 0:
        return np.nan
    base_cutoff = today - timedelta(days=RS_LOOKBACK_CALENDAR_DAYS)
    base_rows = history[history["time"] <= base_cutoff]
    if base_rows.empty:
        return np.nan
    base_close = pd.to_numeric(base_rows.iloc[-1]["close"], errors="coerce")
    if pd.isna(base_close) or base_close <= 0:
        return np.nan
    return (intraday_price / base_close) - 1.0


def _compute_weighted_momentum(history: pd.DataFrame, intraday_price: float) -> float:
    """Weighted 5/10/20-session momentum vs intraday price. Matches
    rs_matrix_3T.calculate_weighted_momentum_score with intraday_price
    substituted as 'current_close'. Shortened from 10/20/60 to 5/10/20 so
    the intraday HH:MM column reflects recent action more aggressively.
    """
    if intraday_price is None or pd.isna(intraday_price) or intraday_price <= 0:
        return np.nan
    if len(history) < 20:
        return np.nan
    weighted_ratio = 0.0
    for lookback, weight in ((5, 0.50), (10, 0.30), (20, 0.20)):
        if len(history) < lookback:
            return np.nan
        base_close = pd.to_numeric(history.iloc[-lookback]["close"], errors="coerce")
        if pd.isna(base_close) or base_close <= 0:
            return np.nan
        weighted_ratio += weight * (intraday_price / base_close)
    return (weighted_ratio - 1.0) * 100.0


def compute_intraday_rs(combined_path: Path, now_ict: datetime) -> dict | None:
    """Build the intraday RS payload. Returns the JSON-ready dict or None on
    failure (caller logs and continues — intraday RS is opportunistic, never
    blocks intraday breadth or the EOD pipeline).
    """
    tickers = _load_rs_universe()
    if not tickers:
        LOGGER.warning("RS universe is empty; skipping intraday RS.")
        return None

    history_long = _load_history_frame(combined_path, tickers)
    history_by_ticker: dict[str, pd.DataFrame] = {
        t: g.copy() for t, g in history_long.groupby("ticker", sort=False)
    }
    LOGGER.info("RS history loaded: %d tickers", len(history_by_ticker))

    prices = _fetch_intraday_prices(tickers)
    LOGGER.info("Intraday prices fetched: %d / %d tickers", len(prices), len(tickers))
    if len(prices) < len(tickers) // 2:
        LOGGER.warning(
            "Only %d/%d intraday prices fetched — refusing to publish intraday RS this tick",
            len(prices), len(tickers),
        )
        return None

    today = now_ict.date()
    rows = []
    for ticker in tickers:
        hist = history_by_ticker.get(ticker)
        if hist is None or hist.empty:
            continue
        pinfo = prices.get(ticker)
        if pinfo is None:
            continue
        intraday_px = pinfo["intraday_price"]
        ref_px = pinfo["ref_price"]

        stock_ret_90d = _compute_return_90d(hist, intraday_px, today)
        wm_score = _compute_weighted_momentum(hist, intraday_px)
        if pd.isna(stock_ret_90d) and pd.isna(wm_score):
            continue
        daily_change_pct = (
            (intraday_px / ref_px - 1.0) * 100.0
            if pd.notna(ref_px) and ref_px > 0
            else np.nan
        )
        rows.append({
            "ticker": ticker,
            "intraday_price": round(float(intraday_px), 4),
            "stock_return_90d": (
                None if pd.isna(stock_ret_90d) else round(float(stock_ret_90d), 6)
            ),
            "weighted_momentum_score": (
                None if pd.isna(wm_score) else round(float(wm_score), 4)
            ),
            "daily_change_pct": (
                None if pd.isna(daily_change_pct) else round(float(daily_change_pct), 2)
            ),
        })

    if not rows:
        LOGGER.warning("No rows survived intraday RS computation; skipping publish.")
        return None

    df = pd.DataFrame(rows)
    df["rs_pct"] = df["stock_return_90d"].rank(method="average", pct=True)
    df["weighted_momentum_pct"] = df["weighted_momentum_score"].rank(method="average", pct=True)
    df["rs_pct_blended"] = (
        0.30 * df["rs_pct"].fillna(0.0) + 0.70 * df["weighted_momentum_pct"].fillna(0.0)
    )
    df["rs_rating"] = (
        ((df["rs_pct_blended"] * 98) + 1).round().clip(1, 99).astype("Int64")
    )

    payload_rows = []
    for r in df.itertuples(index=False):
        payload_rows.append({
            "ticker": r.ticker,
            "rs_rating": int(r.rs_rating) if pd.notna(r.rs_rating) else None,
            "daily_change_pct": r.daily_change_pct,
        })

    payload = {
        "session_date": today.isoformat(),
        "tick_time_ict": now_ict.strftime("%H:%M"),
        "last_updated_ict": now_ict.strftime("%H:%M %d/%m/%Y"),
        "rows": payload_rows,
    }
    return payload


def publish_intraday_rs(payload: dict) -> None:
    """Upload the intraday RS payload to GCS with no-cache headers."""
    from google.cloud import storage
    client = storage.Client()
    blob = client.bucket(GCS_BUCKET).blob(GCS_INTRADAY_RS_KEY)
    blob.cache_control = "no-cache, no-store, must-revalidate"
    blob.upload_from_string(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        content_type="application/json",
    )


def run_intraday_rs(now_ict: datetime, combined_path: Path) -> None:
    """Entry point called from intraday_breadth.py. Idempotent; logs warnings
    on failure rather than raising — intraday RS is supplementary and must
    never break the breadth tick path."""
    try:
        payload = compute_intraday_rs(combined_path, now_ict)
        if payload is None:
            return
        if os.environ.get("INTRADAY_DRY_RUN", "").lower() in ("1", "true", "yes"):
            LOGGER.info(
                "DRY_RUN — would publish intraday RS with %d rows at %s",
                len(payload["rows"]), payload["tick_time_ict"],
            )
            return
        publish_intraday_rs(payload)
        LOGGER.info(
            "intraday_rs_3T.json published: %d tickers at tick %s",
            len(payload["rows"]), payload["tick_time_ict"],
        )
    except Exception as exc:
        LOGGER.warning("Intraday RS update FAILED (non-fatal): %s", exc)


def main() -> int:
    """Standalone CLI for local testing. Use INTRADAY_LOCAL_COMBINED to point
    at a real combined_dataset.csv on disk; otherwise download from GCS."""
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s - %(message)s", datefmt="%H:%M:%S")

    now_ict = datetime.now(ICT)
    local_override = os.environ.get("INTRADAY_LOCAL_COMBINED")
    if local_override:
        combined_path = Path(local_override)
        if not combined_path.exists():
            raise FileNotFoundError(f"INTRADAY_LOCAL_COMBINED not found: {combined_path}")
    else:
        # Reuse intraday_breadth's downloader for parity.
        from intraday_breadth import download_combined_dataset
        combined_path = SCRIPT_DIR / "data" / "_intraday_combined.csv"
        download_combined_dataset(combined_path)

    run_intraday_rs(now_ict, combined_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
