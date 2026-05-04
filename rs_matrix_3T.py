#!/usr/bin/env python3
"""Build a 10-session Relative Strength matrix for the Institutional 3T universe."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from rs_source2 import (
    INDEX_TICKER,
    RS_FIXED_TICKERS_PATH,
    RS_HISTORY_CACHE_DIR,
    RS_LOOKBACK_CALENDAR_DAYS,
    RS_OUTPUT_SESSIONS,
    append_latest_candle_to_cache,
    configure_logging,
    fetch_history,
    fetch_history_direct,
    load_cached_history,
)


SCRIPT_DIR = Path(__file__).resolve().parent
RS_MATRIX_3T_PATH = SCRIPT_DIR / "rs_matrix_3T.csv"
INITIAL_FETCH_BUFFER_DAYS = RS_LOOKBACK_CALENDAR_DAYS + 60

LOGGER = configure_logging("rs_matrix_3t")


def load_universe() -> pd.DataFrame:
    LOGGER.info("Reading universe from rs_fixed_tickers.csv...")
    if not RS_FIXED_TICKERS_PATH.exists():
        raise FileNotFoundError(
            f"Locked universe file not found: {RS_FIXED_TICKERS_PATH}"
        )

    universe_df = pd.read_csv(RS_FIXED_TICKERS_PATH)
    if universe_df.empty or "ticker" not in universe_df.columns:
        raise ValueError(
            "rs_fixed_tickers.csv is empty or missing the 'ticker' column."
        )

    universe_df["ticker"] = universe_df["ticker"].astype(str).str.upper().str.strip()
    universe_df = universe_df[universe_df["ticker"].str.fullmatch(r"[A-Z0-9]{3,10}")]
    universe_df = universe_df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    universe_df["market_cap"] = pd.to_numeric(universe_df.get("market_cap"), errors="coerce")
    universe_df["universe_order"] = np.arange(1, len(universe_df) + 1)
    return universe_df


def prepare_history_frame(history_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if history_df is None or history_df.empty:
        raise RuntimeError(f"{ticker}: empty history frame.")

    prepared = history_df.copy()
    prepared["time"] = pd.to_datetime(prepared["time"], errors="coerce").dt.date
    prepared["close"] = pd.to_numeric(prepared["close"], errors="coerce")
    if "volume" in prepared.columns:
        prepared["volume"] = pd.to_numeric(prepared["volume"], errors="coerce")
    else:
        prepared["volume"] = np.nan

    prepared = prepared.dropna(subset=["time", "close"]).sort_values("time")
    prepared = prepared.drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
    prepared["daily_change_pct"] = prepared["close"].pct_change().mul(100)
    return prepared


def incremental_sync_history(ticker: str, end_date: str) -> tuple[pd.DataFrame, str]:
    cached_df = load_cached_history(ticker)
    if cached_df is None or cached_df.empty:
        start_date = (date.today() - timedelta(days=INITIAL_FETCH_BUFFER_DAYS)).isoformat()
        history_df = fetch_history(ticker, start_date, end_date, LOGGER)
        if history_df is None or history_df.empty:
            raise RuntimeError(f"{ticker}: initial history load failed.")
        return prepare_history_frame(history_df, ticker), "initial_fetch"

    cached_df = prepare_history_frame(cached_df, ticker)
    last_cached_date = cached_df["time"].max()
    target_date = pd.to_datetime(end_date).date()
    if pd.isna(last_cached_date) or last_cached_date >= target_date:
        return cached_df, "cache_hit"

    latest_slice = fetch_history_direct(
        ticker,
        start_date=last_cached_date.isoformat(),
        end_date=end_date,
        logger=LOGGER,
    )
    if latest_slice is None or latest_slice.empty:
        LOGGER.warning(
            "%s: incremental history refresh failed; using cache through %s",
            ticker,
            last_cached_date,
        )
        return cached_df, "cache_hit"

    latest_slice = prepare_history_frame(latest_slice, ticker)
    latest_slice = latest_slice[latest_slice["time"] > last_cached_date]
    if latest_slice.empty:
        return cached_df, "cache_hit"

    merged_history = append_latest_candle_to_cache(ticker, latest_slice)
    return prepare_history_frame(merged_history, ticker), "incremental_append"


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
    """Weighted 10/20/60-session momentum, shown as pct above or below 1.0."""
    history_df = history_df[history_df["time"] <= session_date].sort_values("time")
    if len(history_df) < 61:
        return np.nan

    current_close = pd.to_numeric(history_df.iloc[-1]["close"], errors="coerce")
    if pd.isna(current_close) or current_close <= 0:
        return np.nan

    weighted_ratio = 0.0
    for lookback, weight in ((10, 0.50), (20, 0.30), (60, 0.20)):
        base_close = pd.to_numeric(history_df.iloc[-(lookback + 1)]["close"], errors="coerce")
        if pd.isna(base_close) or base_close <= 0:
            return np.nan
        weighted_ratio += weight * (current_close / base_close)

    return (weighted_ratio - 1.0) * 100.0


def build_rs_matrix(universe_df: pd.DataFrame) -> pd.DataFrame:
    end_date = date.today().isoformat()
    benchmark_df, benchmark_sync_mode = incremental_sync_history(INDEX_TICKER, end_date)
    benchmark_dates = sorted(benchmark_df["time"].dropna().unique())
    session_dates = benchmark_dates[-RS_OUTPUT_SESSIONS:]
    if len(session_dates) < RS_OUTPUT_SESSIONS:
        raise RuntimeError("VNINDEX history does not contain enough trading sessions.")

    LOGGER.info(
        "Locked RS universe loaded: %s tickers | cache dir: %s",
        len(universe_df),
        RS_HISTORY_CACHE_DIR,
    )
    LOGGER.info(
        "RS benchmark sessions: %s",
        ", ".join(pd.Series(session_dates).astype(str).tolist()),
    )

    benchmark_returns = {
        session_date: calculate_return_90d(benchmark_df, session_date)
        for session_date in session_dates
    }

    all_rows: list[dict] = []
    sync_counters = {"cache_hit": 0, "incremental_append": 0, "initial_fetch": 0}
    failed_tickers: list[str] = []

    total = len(universe_df)
    for position, universe_row in enumerate(universe_df.itertuples(index=False), start=1):
        ticker = universe_row.ticker
        LOGGER.info(
            "[Institutional 3T RS] %s/%s | syncing %s",
            position,
            total,
            ticker,
        )
        try:
            history_df, sync_mode = incremental_sync_history(ticker, end_date)
            sync_counters[sync_mode] += 1
        except Exception as exc:
            LOGGER.warning("NON-FATAL: %s history sync failed: %s", ticker, exc)
            failed_tickers.append(ticker)
            continue

        symbol_dates = set(history_df["time"].dropna().tolist())
        for session_date in session_dates:
            if session_date not in symbol_dates:
                continue

            stock_return = calculate_return_90d(history_df, session_date)
            index_return = benchmark_returns.get(session_date, np.nan)
            if pd.isna(stock_return) or pd.isna(index_return):
                continue

            session_row = history_df[history_df["time"] == session_date].iloc[-1]
            weighted_momentum_score = calculate_weighted_momentum_score(history_df, session_date)
            all_rows.append(
                {
                    "ticker": ticker,
                    "company_name": getattr(universe_row, "company_name", None),
                    "exchange": getattr(universe_row, "exchange", None),
                    "industry": getattr(universe_row, "industry", None),
                    "market_cap": getattr(universe_row, "market_cap", np.nan),
                    "universe_order": getattr(universe_row, "universe_order", np.nan),
                    "session_date": session_date,
                    "close": pd.to_numeric(session_row["close"], errors="coerce"),
                    "daily_change_pct": pd.to_numeric(
                        session_row["daily_change_pct"], errors="coerce"
                    ),
                    "weighted_momentum_score": weighted_momentum_score,
                    "stock_return_90d": stock_return,
                    "index_return_90d": index_return,
                    "relative_performance": stock_return - index_return,
                }
            )

    matrix_df = pd.DataFrame(all_rows)
    if matrix_df.empty:
        raise RuntimeError("No rows were generated for rs_matrix_3T.csv.")

    matrix_df["session_date"] = pd.to_datetime(matrix_df["session_date"]).dt.date
    matrix_df["rs_pct"] = matrix_df.groupby("session_date")["relative_performance"].rank(
        method="average",
        pct=True,
    )
    matrix_df["weighted_momentum_pct"] = matrix_df.groupby("session_date")[
        "weighted_momentum_score"
    ].rank(
        method="average",
        pct=True,
    )
    # Blend RS (relative performance) with momentum: 30% RS + 70% momentum
    matrix_df["rs_pct_blended"] = (
        0.30 * matrix_df["rs_pct"] + 0.70 * matrix_df["weighted_momentum_pct"]
    )
    matrix_df["rs_rating"] = (
        ((matrix_df["rs_pct_blended"] * 98) + 1)
        .round()
        .clip(1, 99)
        .astype("Int64")
    )
    matrix_df["weighted_momentum_rating"] = (
        ((matrix_df["weighted_momentum_pct"] * 98) + 1)
        .round()
        .clip(1, 99)
        .astype("Int64")
    )

    latest_session = matrix_df["session_date"].max()
    latest_scores = (
        matrix_df[matrix_df["session_date"] == latest_session][["ticker", "rs_rating"]]
        .rename(columns={"rs_rating": "latest_rs_rating"})
    )
    matrix_df = matrix_df.merge(latest_scores, on="ticker", how="left")
    matrix_df = matrix_df.sort_values(
        ["latest_rs_rating", "market_cap", "universe_order", "ticker", "session_date"],
        ascending=[False, False, True, True, True],
    ).reset_index(drop=True)

    matrix_df.to_csv(RS_MATRIX_3T_PATH, index=False, encoding="utf-8-sig")
    LOGGER.info(
        "Saved rs_matrix_3T.csv with %s rows across %s sessions.",
        len(matrix_df),
        matrix_df["session_date"].nunique(),
    )
    LOGGER.info(
        "Incremental Sync summary | cache hits=%s | appended=%s | initial fetches=%s",
        sync_counters["cache_hit"],
        sync_counters["incremental_append"],
        sync_counters["initial_fetch"],
    )
    LOGGER.info("Benchmark sync mode: %s", benchmark_sync_mode)
    if failed_tickers:
        LOGGER.warning(
            "NON-FATAL summary: %s fixed-universe tickers failed: %s",
            len(failed_tickers),
            ", ".join(failed_tickers),
        )
    return matrix_df


def main() -> None:
    LOGGER.info("Starting Institutional 3T RS matrix build")
    universe_df = load_universe()
    matrix_df = build_rs_matrix(universe_df)
    latest_session = pd.to_datetime(matrix_df["session_date"]).max().date().isoformat()
    leader_slice = matrix_df[matrix_df["session_date"] == pd.to_datetime(latest_session).date()]
    leaders = (
        leader_slice.sort_values(["rs_rating", "market_cap", "ticker"], ascending=[False, False, True])[
            "ticker"
        ]
        .head(10)
        .tolist()
    )
    LOGGER.info(
        "Locked-universe RS complete | latest session=%s | leaders=%s",
        latest_session,
        ", ".join(leaders),
    )


if __name__ == "__main__":
    main()
