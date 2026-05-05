#!/usr/bin/env python3
"""Build a 20-session Relative Strength matrix for the pinned crypto universe.

Mirrors rs_matrix_3T.py (Vietnam stocks vs VNINDEX) but for crypto vs BTC:
  - Source: yfinance daily bars
  - Universe: crypto_universe.csv (top-50 pinned, BTC included as benchmark only)
  - Benchmark: BTC-USD (excluded from rated cohort — it's the denominator)
  - Composite RS Rating: 30% relative-performance percentile + 70% weighted-momentum percentile
  - Output: rs_matrix_crypto.csv (same schema as rs_matrix_3T.csv)
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
CRYPTO_UNIVERSE_PATH = SCRIPT_DIR / "crypto_universe.csv"
RS_MATRIX_CRYPTO_PATH = SCRIPT_DIR / "rs_matrix_crypto.csv"
CACHE_DIR = SCRIPT_DIR / "cache" / "rs_history_crypto"

BENCHMARK_TICKER = "BTC-USD"
RS_LOOKBACK_CALENDAR_DAYS = 90
RS_OUTPUT_SESSIONS = 20
INITIAL_FETCH_BUFFER_DAYS = RS_LOOKBACK_CALENDAR_DAYS + 60
YF_RATE_LIMIT_DELAY = 0.6  # yfinance is more permissive than vnstock; modest pacing

LOGGER = logging.getLogger("rs_matrix_crypto")


def configure_logging() -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%H:%M:%S")
    )
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def load_universe() -> pd.DataFrame:
    LOGGER.info("Reading crypto universe from %s", CRYPTO_UNIVERSE_PATH.name)
    if not CRYPTO_UNIVERSE_PATH.exists():
        raise FileNotFoundError(f"Crypto universe file not found: {CRYPTO_UNIVERSE_PATH}")

    df = pd.read_csv(CRYPTO_UNIVERSE_PATH)
    if df.empty or "ticker" not in df.columns:
        raise ValueError("crypto_universe.csv is empty or missing the 'ticker' column.")

    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    df["market_cap"] = pd.to_numeric(df.get("market_cap"), errors="coerce")
    df["universe_order"] = np.arange(1, len(df) + 1)
    return df


def _cache_path(ticker: str) -> Path:
    safe_name = ticker.replace("/", "_")
    return CACHE_DIR / f"{safe_name}.csv"


def _drop_in_progress_utc_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only finalized UTC daily candles.

    Crypto trades 24/7 and yfinance's daily bar boundary is 00:00 UTC. When
    the pipeline runs at 15:30 ICT (08:30 UTC), the 'today UTC' bar is only
    ~8.5h into its 24h window — i.e. an in-progress partial. Excluding it
    means the heatmap's rightmost column is always the candle that closed
    at 07:00 ICT today, not a mid-day snapshot.
    """
    if df is None or df.empty or "time" not in df.columns:
        return df
    today_utc = datetime.now(timezone.utc).date()
    return df[df["time"] < today_utc].reset_index(drop=True)


def _normalize_yf_frame(raw_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        raise ValueError(f"{ticker}: empty yfinance frame.")

    df = raw_df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]).lower().replace(" ", "_") for col in df.columns.to_flat_index()]
    else:
        df.columns = [str(col).lower().replace(" ", "_") for col in df.columns]

    df = df.reset_index().rename(columns={"Date": "time", "date": "time"})
    if "time" not in df.columns or "close" not in df.columns:
        raise ValueError(f"{ticker}: missing required time/close columns")

    df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.date
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = pd.NA
    df = df[["time", "open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["time", "close"]).sort_values("time").drop_duplicates("time", keep="last")
    df["ticker"] = ticker
    return df.reset_index(drop=True)


def _load_cached_history(ticker: str) -> pd.DataFrame | None:
    cache_path = _cache_path(ticker)
    if not cache_path.exists():
        return None
    try:
        df = pd.read_csv(cache_path)
        return _drop_in_progress_utc_bar(_normalize_yf_frame(df, ticker))
    except Exception:
        return None


def _save_cached_history(ticker: str, df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_cache_path(ticker), index=False, encoding="utf-8-sig")


def _fetch_yf_history(ticker: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """Fetch daily OHLCV via yfinance. Returns None on failure."""
    try:
        import yfinance as yf
        raw = yf.download(
            ticker,
            start=start_date,
            end=end_date,
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        if raw is None or raw.empty:
            return None
        return _drop_in_progress_utc_bar(_normalize_yf_frame(raw, ticker))
    except Exception as exc:
        LOGGER.warning("%s yfinance fetch failed: %s", ticker, exc)
        return None
    finally:
        time.sleep(YF_RATE_LIMIT_DELAY)


def incremental_sync_history(ticker: str) -> tuple[pd.DataFrame, str]:
    """Cache-first sync; appends only new bars when cached data exists."""
    today = date.today()
    end_date = (today + timedelta(days=1)).isoformat()  # yfinance end is exclusive

    cached = _load_cached_history(ticker)
    if cached is None or cached.empty:
        start_date = (today - timedelta(days=INITIAL_FETCH_BUFFER_DAYS)).isoformat()
        fetched = _fetch_yf_history(ticker, start_date, end_date)
        if fetched is None or fetched.empty:
            raise RuntimeError(f"{ticker}: initial yfinance fetch failed")
        _save_cached_history(ticker, fetched)
        return fetched, "initial_fetch"

    last_date = cached["time"].max()
    if pd.isna(last_date) or last_date >= today - timedelta(days=1):
        # Within ~1 day of today; cache is fresh enough for daily-bar RS
        return cached, "cache_hit"

    start_date = last_date.isoformat()
    fresh = _fetch_yf_history(ticker, start_date, end_date)
    if fresh is None or fresh.empty:
        LOGGER.warning("%s: refresh failed; using cache through %s", ticker, last_date)
        return cached, "cache_hit"

    fresh = fresh[fresh["time"] > last_date]
    if fresh.empty:
        return cached, "cache_hit"

    merged = pd.concat([cached, fresh], ignore_index=True)
    merged = merged.drop_duplicates(subset=["time"], keep="last").sort_values("time").reset_index(drop=True)
    _save_cached_history(ticker, merged)
    return merged, "incremental_append"


def calculate_return_90d(history_df: pd.DataFrame, session_date) -> float:
    history_df = history_df.sort_values("time")
    current_rows = history_df[history_df["time"] == session_date]
    if current_rows.empty:
        return np.nan
    current_close = pd.to_numeric(current_rows.iloc[-1]["close"], errors="coerce")
    if pd.isna(current_close) or current_close <= 0:
        return np.nan

    base_cutoff = session_date - timedelta(days=RS_LOOKBACK_CALENDAR_DAYS)
    base_rows = history_df[history_df["time"] <= base_cutoff]
    if base_rows.empty:
        return np.nan
    base_close = pd.to_numeric(base_rows.iloc[-1]["close"], errors="coerce")
    if pd.isna(base_close) or base_close <= 0:
        return np.nan
    return (current_close / base_close) - 1.0


def calculate_weighted_momentum_score(history_df: pd.DataFrame, session_date) -> float:
    """Weighted 10/20/60-session momentum, expressed as pct above/below 1.0."""
    df = history_df[history_df["time"] <= session_date].sort_values("time")
    if len(df) < 61:
        return np.nan
    current_close = pd.to_numeric(df.iloc[-1]["close"], errors="coerce")
    if pd.isna(current_close) or current_close <= 0:
        return np.nan
    weighted_ratio = 0.0
    for lookback, weight in ((10, 0.50), (20, 0.30), (60, 0.20)):
        base_close = pd.to_numeric(df.iloc[-(lookback + 1)]["close"], errors="coerce")
        if pd.isna(base_close) or base_close <= 0:
            return np.nan
        weighted_ratio += weight * (current_close / base_close)
    return (weighted_ratio - 1.0) * 100.0


def build_rs_matrix(universe_df: pd.DataFrame) -> pd.DataFrame:
    benchmark_df, benchmark_sync = incremental_sync_history(BENCHMARK_TICKER)
    benchmark_dates = sorted(benchmark_df["time"].dropna().unique())
    session_dates = benchmark_dates[-RS_OUTPUT_SESSIONS:]
    if len(session_dates) < RS_OUTPUT_SESSIONS:
        raise RuntimeError(
            f"BTC-USD history has only {len(session_dates)} sessions, need {RS_OUTPUT_SESSIONS}"
        )

    LOGGER.info(
        "Crypto RS universe loaded: %s coins | benchmark=%s | cache=%s",
        len(universe_df), BENCHMARK_TICKER, CACHE_DIR,
    )

    benchmark_returns = {
        sd: calculate_return_90d(benchmark_df, sd) for sd in session_dates
    }

    rated_universe = universe_df[universe_df["ticker"] != BENCHMARK_TICKER].copy()
    sync_counters = {"cache_hit": 0, "incremental_append": 0, "initial_fetch": 0}
    failed: list[str] = []
    rows: list[dict] = []

    total = len(rated_universe)
    for pos, row in enumerate(rated_universe.itertuples(index=False), start=1):
        ticker = row.ticker
        LOGGER.info("[Crypto RS] %s/%s | syncing %s", pos, total, ticker)
        try:
            history_df, sync_mode = incremental_sync_history(ticker)
            sync_counters[sync_mode] += 1
        except Exception as exc:
            LOGGER.warning("NON-FATAL: %s sync failed: %s", ticker, exc)
            failed.append(ticker)
            continue

        symbol_dates = set(history_df["time"].dropna().tolist())
        for sd in session_dates:
            if sd not in symbol_dates:
                continue
            stock_ret = calculate_return_90d(history_df, sd)
            idx_ret = benchmark_returns.get(sd, np.nan)
            if pd.isna(stock_ret) or pd.isna(idx_ret):
                continue
            session_row = history_df[history_df["time"] == sd].iloc[-1]
            rows.append({
                "ticker": ticker,
                "company_name": getattr(row, "company_name", None),
                "exchange": getattr(row, "exchange", None),
                "industry": getattr(row, "industry", None),
                "market_cap": getattr(row, "market_cap", np.nan),
                "universe_order": getattr(row, "universe_order", np.nan),
                "session_date": sd,
                "close": pd.to_numeric(session_row["close"], errors="coerce"),
                "daily_change_pct": (
                    pd.to_numeric(session_row.get("close"), errors="coerce")
                    if "close" in session_row.index else np.nan
                ),
                "weighted_momentum_score": calculate_weighted_momentum_score(history_df, sd),
                "stock_return_90d": stock_ret,
                "index_return_90d": idx_ret,
                "relative_performance": stock_ret - idx_ret,
            })

    matrix = pd.DataFrame(rows)
    if matrix.empty:
        raise RuntimeError("No rows generated for rs_matrix_crypto.csv")

    # Recompute daily_change_pct properly per ticker (close pct_change)
    matrix["session_date"] = pd.to_datetime(matrix["session_date"]).dt.date
    matrix = matrix.sort_values(["ticker", "session_date"])
    matrix["daily_change_pct"] = matrix.groupby("ticker")["close"].pct_change() * 100
    matrix["session_date"] = pd.to_datetime(matrix["session_date"]).dt.date

    matrix["rs_pct"] = matrix.groupby("session_date")["relative_performance"].rank(method="average", pct=True)
    matrix["weighted_momentum_pct"] = matrix.groupby("session_date")["weighted_momentum_score"].rank(method="average", pct=True)
    # 30% RS + 70% momentum, matching rs_matrix_3T.py
    matrix["rs_pct_blended"] = 0.30 * matrix["rs_pct"] + 0.70 * matrix["weighted_momentum_pct"]
    matrix["rs_rating"] = (
        ((matrix["rs_pct_blended"] * 98) + 1).round().clip(1, 99).astype("Int64")
    )
    matrix["weighted_momentum_rating"] = (
        ((matrix["weighted_momentum_pct"] * 98) + 1).round().clip(1, 99).astype("Int64")
    )

    latest_session = matrix["session_date"].max()
    latest_scores = (
        matrix[matrix["session_date"] == latest_session][["ticker", "rs_rating"]]
        .rename(columns={"rs_rating": "latest_rs_rating"})
    )
    matrix = matrix.merge(latest_scores, on="ticker", how="left")
    matrix = matrix.sort_values(
        ["latest_rs_rating", "universe_order", "ticker", "session_date"],
        ascending=[False, True, True, True],
    ).reset_index(drop=True)

    matrix.to_csv(RS_MATRIX_CRYPTO_PATH, index=False, encoding="utf-8-sig")
    LOGGER.info(
        "Saved rs_matrix_crypto.csv with %s rows across %s sessions.",
        len(matrix), matrix["session_date"].nunique(),
    )
    LOGGER.info(
        "Sync summary | cache hits=%s | appended=%s | initial fetches=%s | benchmark=%s",
        sync_counters["cache_hit"], sync_counters["incremental_append"],
        sync_counters["initial_fetch"], benchmark_sync,
    )
    if failed:
        LOGGER.warning("NON-FATAL: %s coins failed: %s", len(failed), ", ".join(failed))
    return matrix


def main() -> None:
    configure_logging()
    LOGGER.info("Starting crypto RS matrix build")
    universe_df = load_universe()
    matrix = build_rs_matrix(universe_df)
    latest = matrix["session_date"].max()
    leaders = (
        matrix[matrix["session_date"] == latest]
        .sort_values("rs_rating", ascending=False)["ticker"]
        .head(10).tolist()
    )
    LOGGER.info("Crypto RS complete | latest=%s | leaders=%s", latest, ", ".join(leaders))


if __name__ == "__main__":
    main()
