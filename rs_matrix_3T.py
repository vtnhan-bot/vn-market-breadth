#!/usr/bin/env python3
"""Build the 20-session RS matrix for the Institutional 3T universe.

Reads OHLC history directly from `data/<today>/combined_dataset.csv` (written
by `eod_batch_downloader.py`). No per-ticker `cache/rs_history/` files —
combined_dataset.csv is the single source of truth, which means corporate-
action back-adjustments by vnstock automatically propagate (the downloader
re-fetches the full series each day, ~420 calendar-day window).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from rs_source2 import (
    INDEX_TICKER,
    RS_FIXED_TICKERS_PATH,
    RS_LOOKBACK_CALENDAR_DAYS,
    RS_OUTPUT_SESSIONS,
    configure_logging,
)


SCRIPT_DIR = Path(__file__).resolve().parent
RS_MATRIX_3T_PATH = SCRIPT_DIR / "rs_matrix_3T.csv"

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


def load_history_from_combined(combined_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Slice combined_dataset.csv for one ticker and prepare for RS calc.

    `combined_df` is expected to be already loaded and lightly normalised by
    the caller (ticker column upper-cased, stripped). Returns a per-ticker
    history frame with the same shape `prepare_history_frame` produces, so
    downstream calculate_return_90d / calculate_weighted_momentum_score work
    unchanged from the previous cache-based flow.
    """
    ticker = ticker.upper().strip()
    sub = combined_df[combined_df["ticker"] == ticker]
    if sub.empty:
        raise RuntimeError(f"{ticker}: no rows in combined_dataset.csv")
    return prepare_history_frame(sub.copy(), ticker)


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


def build_rs_matrix(universe_df: pd.DataFrame, combined_path: Path) -> pd.DataFrame:
    LOGGER.info("Loading OHLC from %s ...", combined_path.relative_to(SCRIPT_DIR))
    combined_df = pd.read_csv(combined_path, encoding="utf-8-sig")
    combined_df["ticker"] = combined_df["ticker"].astype(str).str.upper().str.strip()
    LOGGER.info("combined_dataset has %s rows across %s unique tickers",
                len(combined_df), combined_df["ticker"].nunique())

    benchmark_df = load_history_from_combined(combined_df, INDEX_TICKER)
    benchmark_dates = sorted(benchmark_df["time"].dropna().unique())
    session_dates = benchmark_dates[-RS_OUTPUT_SESSIONS:]
    if len(session_dates) < RS_OUTPUT_SESSIONS:
        raise RuntimeError("VNINDEX history does not contain enough trading sessions.")

    LOGGER.info("Locked RS universe loaded: %s tickers", len(universe_df))
    LOGGER.info("RS benchmark sessions: %s",
                ", ".join(pd.Series(session_dates).astype(str).tolist()))

    benchmark_returns = {
        session_date: calculate_return_90d(benchmark_df, session_date)
        for session_date in session_dates
    }

    all_rows: list[dict] = []
    failed_tickers: list[str] = []

    total = len(universe_df)
    for position, universe_row in enumerate(universe_df.itertuples(index=False), start=1):
        ticker = universe_row.ticker
        LOGGER.info("[Institutional 3T RS] %s/%s | %s", position, total, ticker)
        try:
            history_df = load_history_from_combined(combined_df, ticker)
        except Exception as exc:
            LOGGER.warning("NON-FATAL: %s history load failed: %s", ticker, exc)
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
        "Saved rs_matrix_3T.csv with %s rows across %s sessions",
        len(matrix_df),
        matrix_df["session_date"].nunique(),
    )
    if failed_tickers:
        LOGGER.warning(
            "NON-FATAL summary: %s tickers failed history load: %s",
            len(failed_tickers),
            ", ".join(failed_tickers),
        )
    return matrix_df


def main() -> None:
    LOGGER.info("Starting Institutional 3T RS matrix build")
    universe_df = load_universe()
    candidates = sorted((SCRIPT_DIR / "data").glob("*/combined_dataset.csv"))
    if not candidates:
        raise RuntimeError(
            "No combined_dataset.csv found under data/<date>/. "
            "Run eod_batch_downloader.py first."
        )
    combined_path = candidates[-1]
    matrix_df = build_rs_matrix(universe_df, combined_path)
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
