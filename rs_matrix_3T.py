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

import liquidity_screen
from rs_source2 import (
    INDEX_TICKER,
    RS_BLEND_RS_WEIGHT,
    RS_FIXED_TICKERS_PATH,
    RS_LOOKBACK_CALENDAR_DAYS,
    RS_MOMENTUM_WINDOWS,
    RS_OUTPUT_SESSIONS,
    configure_logging,
)


SCRIPT_DIR = Path(__file__).resolve().parent
RS_MATRIX_3T_PATH = SCRIPT_DIR / "rs_matrix_3T.csv"
# Kept/dropped membership from the liquidity screen. Written here (EOD), re-read by
# intraday_rs_3T (parity) and market_breadth (alignment audit). See liquidity_screen.
RS_SCREEN_MEMBERS_PATH = SCRIPT_DIR / "rs_screen_members.csv"

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


def calculate_benchmark_return_90d(history_df: pd.DataFrame, session_date) -> float:
    """Benchmark 90-day return, tolerant of a single bad base bar.

    Same math as calculate_return_90d, but the base-bar lookup skips any
    NaN/<=0 close at/just before the cutoff instead of returning NaN. A bad
    VNINDEX base bar would otherwise blank EVERY ticker's cell for that session
    (all rows hit `if pd.isna(index_return): continue`). Only the benchmark is
    hardened here — a single stock nulling its own cell is fine, so the stock
    path (calculate_return_90d) is intentionally left unchanged.
    """
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

    base_closes = pd.to_numeric(base_rows["close"], errors="coerce")
    valid_base = base_closes[base_closes.notna() & (base_closes > 0)]
    if valid_base.empty:
        LOGGER.warning(
            "Benchmark %s: no valid base bar at/before %s for session %s; "
            "session column will be blank.",
            INDEX_TICKER, base_cutoff, session_date,
        )
        return np.nan

    base_close = float(valid_base.iloc[-1])
    naive_base = base_closes.iloc[-1]
    if pd.isna(naive_base) or naive_base <= 0:
        LOGGER.warning(
            "Benchmark %s: base bar at/before %s (session %s) was invalid (%s); "
            "using nearest valid prior close %.4f instead of blanking the column.",
            INDEX_TICKER, base_cutoff, session_date, naive_base, base_close,
        )

    return (current_close / base_close) - 1.0


def calculate_weighted_momentum_score(history_df: pd.DataFrame, session_date) -> float:
    """Weighted multi-session momentum, shown as pct above or below 1.0.

    Windows and weights come from RS_MOMENTUM_WINDOWS (rs_source2). Current
    profile 3/5/10 @ 50/30/20 (the "recency" tuning, 2026-08-02): mean-weighted
    lookback ~4.6 sessions, down from ~9.5 at 5/10/20. intraday_rs_3T mirrors
    this window set exactly.
    """
    history_df = history_df[history_df["time"] <= session_date].sort_values("time")
    min_sessions = max(lb for lb, _ in RS_MOMENTUM_WINDOWS) + 1
    if len(history_df) < min_sessions:
        return np.nan

    current_close = pd.to_numeric(history_df.iloc[-1]["close"], errors="coerce")
    if pd.isna(current_close) or current_close <= 0:
        return np.nan

    weighted_ratio = 0.0
    for lookback, weight in RS_MOMENTUM_WINDOWS:
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
        # tolerant base-bar lookup: a single bad VNINDEX bar must not blank the
        # whole session column for every ticker (see calculate_benchmark_return_90d).
        session_date: calculate_benchmark_return_90d(benchmark_df, session_date)
        for session_date in session_dates
    }

    # --- Liquidity screen -----------------------------------------------------
    # Drop illiquid names from the RANK cohort so the heatmap carries no blank
    # cells / no low-quality tickers, and persist the membership so intraday_rs_3T
    # ranks over the IDENTICAL set (parity) and market_breadth's alignment audit
    # can expect the screened subset. FAIL-SAFE: if the screen would keep too few
    # (e.g. a volume-feed regression), keep the FULL universe and leave the prior
    # members file untouched -- an illiquid name beats blanking or a 95-ticker abort.
    screen_df = combined_df[["ticker", "time", "close", "volume"]].copy()
    screen_df["time"] = pd.to_datetime(screen_df["time"], errors="coerce").dt.date
    screen_sessions = benchmark_dates[-liquidity_screen.SCREEN_WINDOW:]
    prior_members = liquidity_screen.read_kept_members(RS_SCREEN_MEMBERS_PATH)
    kept, dropped, screen_stats = liquidity_screen.screen_universe(
        screen_df, universe_df["ticker"].tolist(), screen_sessions, prior_members=prior_members,
    )
    if len(kept) < liquidity_screen.FAILSAFE_MIN_KEPT:
        # Something is wrong (e.g. a volume-feed regression). Keep the FULL universe
        # AND write an all-"failsafe" membership so market_breadth + intraday also
        # fall back to full this run -- otherwise they would screen against a stale
        # prior members file and disagree with the (now-full) matrix.
        LOGGER.error(
            "Liquidity screen kept only %s (< %s floor) -- FAILING SAFE to the full universe "
            "this run (all-failsafe membership written).",
            len(kept), liquidity_screen.FAILSAFE_MIN_KEPT,
        )
        # Mark rows "failsafe", NOT "kept": read_kept_members counts only
        # kept/coldstart as members, so an all-"failsafe" file reads back as None
        # -> the consumers (intraday_rs_3T, market_breadth) still fall back to the
        # FULL universe this run (parity preserved, same as before), AND the NEXT
        # run's prior_members is None so every name is re-judged by the strict
        # ENTRY band. Writing "kept" here instead grandfathered every name into the
        # loose KEEP band next run -- one transient glitch permanently re-admitted
        # ENTRY/KEEP-gap illiquid names.
        failsafe_stats = screen_stats.copy()
        failsafe_stats["status"] = "failsafe"
        liquidity_screen.write_members(
            RS_SCREEN_MEMBERS_PATH, failsafe_stats, screened_at=str(session_dates[-1]),
        )
    else:
        liquidity_screen.write_members(
            RS_SCREEN_MEMBERS_PATH, screen_stats, screened_at=str(session_dates[-1]),
        )
        if dropped:
            LOGGER.info(
                "Liquidity screen: kept %s, dropped %s illiquid -> %s",
                len(kept), len(dropped), ", ".join(sorted(dropped)),
            )
        else:
            LOGGER.info("Liquidity screen: kept %s, dropped 0.", len(kept))
        universe_df = universe_df[universe_df["ticker"].isin(set(kept))].reset_index(drop=True)

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
    # Blend RS (relative performance) with momentum. RS_BLEND_RS_WEIGHT on the
    # vs-VNINDEX anchor, the remainder on short-window momentum (0.20 / 0.80).
    matrix_df["rs_pct_blended"] = (
        RS_BLEND_RS_WEIGHT * matrix_df["rs_pct"]
        + (1.0 - RS_BLEND_RS_WEIGHT) * matrix_df["weighted_momentum_pct"]
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

    # Per-ticker latest rating keyed on each ticker's OWN most-recent session
    # that has a real rs_rating -- NOT the global latest session. A ticker
    # halted on the latest session (e.g. STG/CRV) is absent from that session's
    # slice, so keying on the global latest alone gives it latest_rs_rating=NaN
    # and sinks its entire 20-session row to the bottom of the heatmap.
    latest_scores = (
        matrix_df[matrix_df["rs_rating"].notna()]
        .sort_values("session_date")
        .drop_duplicates("ticker", keep="last")[["ticker", "rs_rating"]]
        .rename(columns={"rs_rating": "latest_rs_rating"})
    )
    matrix_df = matrix_df.merge(latest_scores, on="ticker", how="left")
    # Sort-only helper: 59/229 tickers have NaN market_cap. Coalesce to 0 so a
    # rating tie breaks by cap deterministically instead of relying on pandas'
    # na_position handling; NOT written to the CSV (market_cap value preserved).
    matrix_df["_market_cap_sort"] = matrix_df["market_cap"].fillna(0)
    matrix_df = matrix_df.sort_values(
        ["latest_rs_rating", "_market_cap_sort", "universe_order", "ticker", "session_date"],
        ascending=[False, False, True, True, True],
    ).reset_index(drop=True)
    matrix_df = matrix_df.drop(columns=["_market_cap_sort"])

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
    # Coalesce NaN market_cap to 0 for the cap tie-break (see build_rs_matrix sort).
    leaders = (
        leader_slice.assign(_market_cap_sort=leader_slice["market_cap"].fillna(0))
        .sort_values(["rs_rating", "_market_cap_sort", "ticker"], ascending=[False, False, True])[
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
