#!/usr/bin/env python3
"""Build the 10-session Relative Strength matrix for Source 2."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from rs_source2 import (
    INDEX_TICKER,
    RS_LOOKBACK_CALENDAR_DAYS,
    RS_FIXED_TICKERS_PATH,
    RS_MATRIX_DATA_PATH,
    RS_OUTPUT_SESSIONS,
    SOURCE2_SOURCE,
    append_latest_candle_to_cache,
    configure_logging,
    fetch_history,
    fetch_history_direct,
    load_cached_history,
)


LOGGER = configure_logging("rs_matrix_builder")


def load_universe() -> pd.DataFrame:
    if not RS_FIXED_TICKERS_PATH.exists():
        raise FileNotFoundError(f"RS fixed universe file not found: {RS_FIXED_TICKERS_PATH}")
    universe_df = pd.read_csv(RS_FIXED_TICKERS_PATH)
    if universe_df.empty or "ticker" not in universe_df.columns:
        raise ValueError("RS fixed universe file is empty or missing the 'ticker' column.")
    universe_df["ticker"] = universe_df["ticker"].astype(str).str.upper()
    return universe_df


def _get_history_frame(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    history_df = fetch_history(ticker, start_date, end_date, LOGGER)
    if history_df is None or history_df.empty:
        raise RuntimeError(f"Unable to load history for {ticker}")
    history_df = history_df.sort_values("time").reset_index(drop=True)
    history_df["time"] = pd.to_datetime(history_df["time"]).dt.date
    history_df["close"] = pd.to_numeric(history_df["close"], errors="coerce")
    history_df["volume"] = pd.to_numeric(history_df["volume"], errors="coerce")
    history_df["daily_change_pct"] = history_df["close"].pct_change().mul(100)
    return history_df


def _progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "[..........]"
    filled = int((current / total) * width)
    filled = max(0, min(width, filled))
    return "[" + ("#" * filled) + ("." * (width - filled)) + "]"


def _log_progress(current: int, total: int, ticker: str) -> None:
    percentage = int((current / total) * 100) if total else 0
    LOGGER.info(
        "[Step 2/4] Updating RS Matrix: %s %s%% (Ticker: %s)",
        _progress_bar(current, total),
        percentage,
        ticker,
    )


def incremental_update_history(ticker: str, benchmark_end_date: str) -> tuple[pd.DataFrame, bool]:
    cached_df = load_cached_history(ticker)
    if cached_df is None or cached_df.empty:
        start_date = (date.today() - timedelta(days=RS_LOOKBACK_CALENDAR_DAYS + 45)).isoformat()
        history_df = fetch_history(ticker, start_date, benchmark_end_date, LOGGER)
        if history_df is None or history_df.empty:
            raise RuntimeError(f"{ticker}: initial history load failed.")
        return _get_history_frame(ticker, start_date, benchmark_end_date), True

    cached_df = cached_df.sort_values("time").reset_index(drop=True)
    cached_df["time"] = pd.to_datetime(cached_df["time"]).dt.date
    last_cached_date = cached_df["time"].max()
    today_date = pd.to_datetime(benchmark_end_date).date()
    if pd.isna(last_cached_date) or last_cached_date >= today_date:
        cached_df["close"] = pd.to_numeric(cached_df["close"], errors="coerce")
        cached_df["volume"] = pd.to_numeric(cached_df["volume"], errors="coerce")
        cached_df["daily_change_pct"] = cached_df["close"].pct_change().mul(100)
        return cached_df, False

    latest_candle_df = fetch_history_direct(
        ticker,
        start_date=last_cached_date.isoformat(),
        end_date=benchmark_end_date,
        logger=LOGGER,
    )
    if latest_candle_df is None or latest_candle_df.empty:
        raise RuntimeError(f"{ticker}: incremental candle update failed.")

    latest_candle_df = latest_candle_df.sort_values("time")
    latest_candle_df["time"] = pd.to_datetime(latest_candle_df["time"]).dt.date
    latest_candle_df = latest_candle_df[latest_candle_df["time"] > last_cached_date]
    if latest_candle_df.empty:
        cached_df["close"] = pd.to_numeric(cached_df["close"], errors="coerce")
        cached_df["volume"] = pd.to_numeric(cached_df["volume"], errors="coerce")
        cached_df["daily_change_pct"] = cached_df["close"].pct_change().mul(100)
        return cached_df, True

    merged_history = append_latest_candle_to_cache(ticker, latest_candle_df)
    merged_history["time"] = pd.to_datetime(merged_history["time"]).dt.date
    merged_history["close"] = pd.to_numeric(merged_history["close"], errors="coerce")
    merged_history["volume"] = pd.to_numeric(merged_history["volume"], errors="coerce")
    merged_history["daily_change_pct"] = merged_history["close"].pct_change().mul(100)
    return merged_history, True


def _return_over_90_calendar_days(history_df: pd.DataFrame, current_date) -> float:
    history_df = history_df.sort_values("time")
    current_rows = history_df[history_df["time"] == current_date]
    if current_rows.empty:
        return np.nan

    current_close = pd.to_numeric(current_rows.iloc[-1]["close"], errors="coerce")
    if pd.isna(current_close) or current_close <= 0:
        return np.nan

    cutoff_date = current_date - timedelta(days=RS_LOOKBACK_CALENDAR_DAYS)
    base_rows = history_df[history_df["time"] <= cutoff_date]
    if base_rows.empty:
        return np.nan

    base_close = pd.to_numeric(base_rows.iloc[-1]["close"], errors="coerce")
    if pd.isna(base_close) or base_close <= 0:
        return np.nan

    return (current_close / base_close) - 1.0


def build_rs_matrix(universe_df: pd.DataFrame) -> pd.DataFrame:
    end_date = date.today().isoformat()
    benchmark_df, benchmark_used_api = incremental_update_history(INDEX_TICKER, end_date)
    benchmark_dates = sorted(benchmark_df["time"].dropna().unique())
    session_dates = benchmark_dates[-RS_OUTPUT_SESSIONS:]
    if len(session_dates) < RS_OUTPUT_SESSIONS:
        raise RuntimeError("VNINDEX history does not contain enough sessions for the RS matrix.")

    LOGGER.info("RS benchmark sessions: %s", ", ".join(pd.Series(session_dates).astype(str).tolist()))

    all_rows: list[dict] = []
    failed_tickers: list[str] = []
    updated_from_cache = 0
    fetched_from_api = 0
    tickers = universe_df["ticker"].tolist()
    total = len(tickers)
    for idx, ticker in enumerate(tickers, start=1):
        _log_progress(idx, total, ticker)
        try:
            history_df, used_api = incremental_update_history(ticker, end_date)
            if used_api:
                fetched_from_api += 1
            else:
                updated_from_cache += 1
        except Exception as exc:
            LOGGER.warning("NON-FATAL ERROR: %s RS history unavailable: %s", ticker, exc)
            failed_tickers.append(ticker)
            continue

        symbol_dates = set(history_df["time"].dropna().tolist())
        for session_date in session_dates:
            if session_date not in symbol_dates:
                continue
            stock_return = _return_over_90_calendar_days(history_df, session_date)
            index_return = _return_over_90_calendar_days(benchmark_df, session_date)
            if pd.isna(stock_return) or pd.isna(index_return):
                continue

            session_row = history_df[history_df["time"] == session_date].iloc[-1]
            all_rows.append(
                {
                    "ticker": ticker,
                    "session_date": session_date,
                    "daily_change_pct": pd.to_numeric(session_row["daily_change_pct"], errors="coerce"),
                    "stock_return_90d": stock_return,
                    "index_return_90d": index_return,
                    "relative_performance": stock_return - index_return,
                    "close": pd.to_numeric(session_row["close"], errors="coerce"),
                    "source": SOURCE2_SOURCE,
                }
            )

    matrix_df = pd.DataFrame(all_rows)
    if matrix_df.empty:
        raise RuntimeError("No RS matrix rows could be calculated.")

    matrix_df["session_date"] = pd.to_datetime(matrix_df["session_date"]).dt.date
    matrix_df["rs_pct"] = matrix_df.groupby("session_date")["relative_performance"].rank(
        method="average", pct=True
    )
    matrix_df["rs_rating"] = ((matrix_df["rs_pct"] * 98) + 1).round().clip(1, 99).astype(int)

    latest_scores = matrix_df[matrix_df["session_date"] == matrix_df["session_date"].max()][
        ["ticker", "rs_rating"]
    ].rename(columns={"rs_rating": "latest_rs_rating"})
    matrix_df = matrix_df.merge(latest_scores, on="ticker", how="left")
    matrix_df = matrix_df.merge(
        universe_df[["ticker", "universe_rank", "combined_score"]],
        on="ticker",
        how="left",
    )
    matrix_df = matrix_df.sort_values(
        ["latest_rs_rating", "combined_score", "ticker", "session_date"],
        ascending=[False, False, True, True],
    ).reset_index(drop=True)
    matrix_df.to_csv(RS_MATRIX_DATA_PATH, index=False, encoding="utf-8-sig")
    LOGGER.info("Saved RS matrix data: %s (%s rows)", RS_MATRIX_DATA_PATH, len(matrix_df))
    LOGGER.info(
        "Incremental Sync: %s tickers updated from cache, %s tickers fetched from API.",
        updated_from_cache,
        fetched_from_api,
    )
    if benchmark_used_api:
        LOGGER.info("Incremental Sync benchmark: VNINDEX required an API refresh.")
    else:
        LOGGER.info("Incremental Sync benchmark: VNINDEX served from cache.")
    if failed_tickers:
        LOGGER.warning(
            "NON-FATAL ERROR summary: %s fixed tickers failed during RS sync: %s",
            len(failed_tickers),
            ", ".join(failed_tickers),
        )
    return matrix_df


def main() -> None:
    LOGGER.info("Starting Source 2 RS matrix build")
    universe_df = load_universe()
    matrix_df = build_rs_matrix(universe_df)
    latest_session = pd.to_datetime(matrix_df["session_date"]).max().date().isoformat()
    top_names = (
        matrix_df[matrix_df["session_date"] == pd.to_datetime(latest_session).date()]
        .sort_values(["rs_rating", "ticker"], ascending=[False, True])["ticker"]
        .head(10)
        .tolist()
    )
    LOGGER.info("RS matrix complete | latest session=%s | leaders=%s", latest_session, ", ".join(top_names))


if __name__ == "__main__":
    main()
