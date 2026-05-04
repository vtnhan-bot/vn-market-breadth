#!/usr/bin/env python3
"""Shared helpers for the Source 2 Relative Strength pipeline."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
from vnstock import Company, Listing, Vnstock


SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
DATA_DIR = SCRIPT_DIR / "data"
RS_FIXED_TICKERS_PATH = SCRIPT_DIR / "rs_fixed_tickers.csv"  # unified canonical universe
RS_MATRIX_DATA_PATH = SCRIPT_DIR / "rs_matrix_data.csv"
RS_METADATA_CACHE_PATH = CACHE_DIR / "rs_company_overview_cache.csv"
RS_HISTORY_CACHE_DIR = CACHE_DIR / "rs_history"
RS_ARCHIVE_DIR = CACHE_DIR / "archive"

SOURCE2_SOURCE = "KBS"
RS_RATE_LIMIT_DELAY_SECONDS = 1.1
UNIVERSE_LIMIT = 200
UNIVERSE_LOOKBACK_DAYS = 120
RS_LOOKBACK_CALENDAR_DAYS = 90
RS_OUTPUT_SESSIONS = 20
COMPANY_CACHE_REFRESH_DAYS = 30
INDEX_TICKER = "VNINDEX"
TARGET_EXCHANGES = {"HOSE", "HNX"}


@dataclass(frozen=True)
class UniverseCandidate:
    ticker: str
    exchange: str


def configure_logging(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def ensure_directories() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    RS_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_listing_universe(logger: logging.Logger) -> list[UniverseCandidate]:
    ensure_directories()
    listing = Listing(source=SOURCE2_SOURCE)
    df = listing.symbols_by_exchange()
    if df is None or df.empty:
        raise RuntimeError("Listing.symbols_by_exchange() returned no rows.")

    df.columns = [str(col).lower() for col in df.columns]
    if "symbol" not in df.columns or "exchange" not in df.columns:
        raise ValueError("Listing dataset is missing required symbol/exchange columns.")

    if "type" in df.columns:
        df = df[df["type"].astype(str).str.lower() == "stock"]

    df["exchange"] = df["exchange"].astype(str).str.upper().str.strip()
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df = df[df["exchange"].isin(TARGET_EXCHANGES)]
    df = df[df["symbol"].str.fullmatch(r"[A-Z]{3,4}")]
    df = df.drop_duplicates(subset=["symbol"], keep="first").sort_values("symbol")

    candidates = [
        UniverseCandidate(ticker=row.symbol, exchange=row.exchange)
        for row in df.itertuples(index=False)
    ]
    logger.info("Source 2 candidate universe: %s symbols across HOSE/HNX", len(candidates))
    return candidates


def _history_cache_path(ticker: str) -> Path:
    return RS_HISTORY_CACHE_DIR / f"{ticker}.csv"


def archive_rs_cache_file(ticker: str) -> Path | None:
    ensure_directories()
    source_path = _history_cache_path(ticker)
    if not source_path.exists():
        return None

    archive_path = RS_ARCHIVE_DIR / source_path.name
    if archive_path.exists():
        archive_path.unlink()
    shutil.move(str(source_path), str(archive_path))
    return archive_path


def normalize_history_frame(raw_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        raise ValueError(f"{ticker}: history payload is empty.")

    df = raw_df.copy()
    column_map = {str(col).lower(): col for col in df.columns}
    required = {"time", "close"}
    if not required.issubset(column_map):
        raise ValueError(f"{ticker}: history missing required columns {sorted(required)}")

    rename_map = {
        column_map["time"]: "time",
        column_map["close"]: "close",
    }
    for optional_column in ("open", "high", "low", "volume"):
        if optional_column in column_map:
            rename_map[column_map[optional_column]] = optional_column

    df = df.rename(columns=rename_map)
    df["time"] = pd.to_datetime(df["time"], unit="ms", errors="coerce").fillna(
        pd.to_datetime(df["time"], errors="coerce")
    )
    df["time"] = df["time"].dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    for optional_column in ("open", "high", "low", "volume"):
        if optional_column in df.columns:
            df[optional_column] = pd.to_numeric(df[optional_column], errors="coerce")
        else:
            df[optional_column] = pd.NA

    df = df[["time", "open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["time", "close"]).sort_values("time").drop_duplicates("time", keep="last")
    df["ticker"] = ticker
    return df.reset_index(drop=True)


def load_cached_history(ticker: str) -> pd.DataFrame | None:
    cache_path = _history_cache_path(ticker)
    if not cache_path.exists():
        return None
    try:
        df = pd.read_csv(cache_path)
        return normalize_history_frame(df, ticker)
    except Exception:
        return None


def save_history_cache(ticker: str, df: pd.DataFrame) -> None:
    ensure_directories()
    df.to_csv(_history_cache_path(ticker), index=False, encoding="utf-8-sig")


def append_latest_candle_to_cache(ticker: str, candle_df: pd.DataFrame) -> pd.DataFrame:
    existing_df = load_cached_history(ticker)
    frames = []
    if existing_df is not None and not existing_df.empty:
        frames.append(existing_df)
    if candle_df is not None and not candle_df.empty:
        frames.append(candle_df)

    if not frames:
        raise ValueError(f"{ticker}: no history available to save.")

    combined_df = pd.concat(frames, ignore_index=True)
    combined_df = normalize_history_frame(combined_df, ticker)
    save_history_cache(ticker, combined_df)
    return combined_df


def fetch_history(
    ticker: str,
    start_date: str,
    end_date: str,
    logger: logging.Logger,
) -> pd.DataFrame | None:
    cached_df = load_cached_history(ticker)
    if cached_df is not None and not cached_df.empty:
        return cached_df

    try:
        stock = Vnstock().stock(symbol=ticker, source=SOURCE2_SOURCE)
        raw_df = stock.quote.history(start=start_date, end=end_date, interval="1D")
        df = normalize_history_frame(raw_df, ticker)
        save_history_cache(ticker, df)
        logger.info("%s history cached (%s rows)", ticker, len(df))
        return df
    except Exception as exc:
        logger.warning("%s history fetch failed: %s", ticker, exc)
        return None
    finally:
        time.sleep(RS_RATE_LIMIT_DELAY_SECONDS)


def fetch_history_direct(
    ticker: str,
    start_date: str,
    end_date: str,
    logger: logging.Logger,
) -> pd.DataFrame | None:
    try:
        stock = Vnstock().stock(symbol=ticker, source=SOURCE2_SOURCE)
        raw_df = stock.quote.history(start=start_date, end=end_date, interval="1D")
        df = normalize_history_frame(raw_df, ticker)
        logger.info("%s direct history fetch returned %s rows", ticker, len(df))
        return df
    except Exception as exc:
        logger.warning("%s direct history fetch failed: %s", ticker, exc)
        return None
    finally:
        time.sleep(RS_RATE_LIMIT_DELAY_SECONDS)


def load_metadata_cache() -> pd.DataFrame:
    if not RS_METADATA_CACHE_PATH.exists():
        return pd.DataFrame(
            columns=[
                "ticker",
                "exchange",
                "outstanding_shares",
                "listed_volume",
                "cached_at",
            ]
        )

    df = pd.read_csv(RS_METADATA_CACHE_PATH)
    if df.empty:
        return df
    if "cached_at" in df.columns:
        df["cached_at"] = pd.to_datetime(df["cached_at"], errors="coerce")
    return df


def save_metadata_cache(df: pd.DataFrame) -> None:
    ensure_directories()
    df.to_csv(RS_METADATA_CACHE_PATH, index=False, encoding="utf-8-sig")


def _is_cache_stale(cached_at: pd.Timestamp | None) -> bool:
    if cached_at is None or pd.isna(cached_at):
        return True
    age = datetime.now() - cached_at.to_pydatetime()
    return age > timedelta(days=COMPANY_CACHE_REFRESH_DAYS)


def update_metadata_cache(
    candidates: Iterable[UniverseCandidate],
    logger: logging.Logger,
) -> pd.DataFrame:
    cache_df = load_metadata_cache()
    cache_by_ticker = {
        str(row["ticker"]).upper(): row
        for _, row in cache_df.iterrows()
        if pd.notna(row.get("ticker"))
    }

    refreshed_rows: list[dict] = []
    for candidate in candidates:
        cached_row = cache_by_ticker.get(candidate.ticker)
        if cached_row is not None and not _is_cache_stale(cached_row.get("cached_at")):
            refreshed_rows.append(dict(cached_row))
            continue

        try:
            overview_df = Company(symbol=candidate.ticker, source=SOURCE2_SOURCE).overview()
            overview = overview_df.iloc[0].to_dict() if overview_df is not None and not overview_df.empty else {}
            refreshed_rows.append(
                {
                    "ticker": candidate.ticker,
                    "exchange": overview.get("exchange", candidate.exchange),
                    "outstanding_shares": pd.to_numeric(
                        overview.get("outstanding_shares"), errors="coerce"
                    ),
                    "listed_volume": pd.to_numeric(
                        overview.get("listed_volume"), errors="coerce"
                    ),
                    "cached_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            logger.info("%s overview cached", candidate.ticker)
        except Exception as exc:
            logger.warning("%s overview fetch failed: %s", candidate.ticker, exc)
            fallback_row = {
                "ticker": candidate.ticker,
                "exchange": candidate.exchange,
                "outstanding_shares": pd.NA,
                "listed_volume": pd.NA,
                "cached_at": datetime.now().isoformat(timespec="seconds"),
            }
            if cached_row is not None:
                fallback_row.update(dict(cached_row))
            refreshed_rows.append(fallback_row)
        finally:
            time.sleep(RS_RATE_LIMIT_DELAY_SECONDS)

    refreshed_df = pd.DataFrame(refreshed_rows)
    refreshed_df["ticker"] = refreshed_df["ticker"].astype(str).str.upper()
    refreshed_df["cached_at"] = pd.to_datetime(refreshed_df["cached_at"], errors="coerce")
    refreshed_df = refreshed_df.drop_duplicates(subset=["ticker"], keep="last").sort_values("ticker")
    save_metadata_cache(refreshed_df)
    return refreshed_df


def percentile_rank(series: pd.Series) -> pd.Series:
    if series.empty:
        return series
    return series.rank(method="average", pct=True)
