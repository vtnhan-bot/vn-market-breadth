#!/usr/bin/env python3
"""End-of-day batch downloader for market breadth inputs.

This script prioritizes reliability over speed:
1. Uses a daily on-disk cache in ./data/YYYY-MM-DD/
2. Enforces deterministic pacing after every API call
3. Retries failed requests with a 60-second cooldown
4. Saves each successful ticker immediately
5. Compiles a final dataset and validates ticker coverage
"""

from __future__ import annotations
import sys
import io

# Force UTF-8 for Windows console/pipe handling
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv
from vnstock import Vnstock


SCRIPT_DIR = Path(__file__).resolve().parent
TICKERS_FILE = SCRIPT_DIR / "tickers.csv"
DATA_DIR = SCRIPT_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
ICT = ZoneInfo("Asia/Ho_Chi_Minh")

API_SOURCES = ["KBS", "VCI", "MSN", "FMP"]  # vnstock 3.5.0+ dropped SSI/VND; community-tier limit is 60 req/min so 1.1s spacing fits
API_CALL_DELAY_SECONDS = 1.1
ERROR_BACKOFF_SECONDS = 60
FETCH_DAYS_BACK = 420
MIN_VALID_TICKERS = 95
INDEX_TICKER = "VNINDEX"

LOGGER = logging.getLogger("eod_batch_downloader")


@dataclass(frozen=True)
class FetchResult:
    ticker: str
    status: str
    dataframe: pd.DataFrame | None
    detail: str


def configure_logging() -> None:
    """Configure console logging for batch execution."""
    if LOGGER.handlers:
        return

    LOGGER.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%H:%M:%S")
    )
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def setup_vnstock_api_key() -> bool:
    """Register VNstock API key from environment when available."""
    load_dotenv()
    api_key = os.environ.get("VNSTOCK_API_KEY", "").strip()
    if not api_key:
        LOGGER.info("VNSTOCK_API_KEY not found in .env; continuing with guest/community access.")
        return False

    try:
        from vnstock import register_user

        registered = register_user(api_key)
        if registered:
            masked_key = f"{api_key[:4]}***{api_key[-4:]}" if len(api_key) > 8 else "****"
            LOGGER.info("VNstock API key loaded from .env: %s", masked_key)
            return True
        LOGGER.warning("VNstock API key was provided but could not be registered.")
    except Exception as exc:
        LOGGER.warning("VNstock API key setup failed: %s", exc)
    return False


def read_tickers(limit: int = 100) -> list[str]:
    """Read ticker symbols from tickers.csv."""
    if not TICKERS_FILE.exists():
        raise FileNotFoundError(f"Ticker file not found: {TICKERS_FILE}")

    df = pd.read_csv(TICKERS_FILE)
    if "Ticker" not in df.columns:
        raise ValueError("tickers.csv must contain a 'Ticker' column.")

    tickers = df["Ticker"].dropna().astype(str).str.strip()
    tickers = [ticker for ticker in tickers if ticker and ticker.lower() != "nan"]
    tickers = tickers[:limit]
    if INDEX_TICKER not in tickers:
        tickers.append(INDEX_TICKER)
    return tickers


def get_today_cache_dir() -> Path:
    """Return today's cache directory, creating it if needed."""
    today_dir = DATA_DIR / date.today().isoformat()
    today_dir.mkdir(parents=True, exist_ok=True)
    return today_dir


def archive_previous_day_cache() -> None:
    """Archive prior daily cache directories before the scheduled EOD run."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now(ICT).date().isoformat()

    for path in DATA_DIR.iterdir():
        if not path.is_dir() or path.name in {"archive", today_str}:
            continue
        try:
            datetime.strptime(path.name, "%Y-%m-%d")
        except ValueError:
            continue

        archive_target = ARCHIVE_DIR / path.name
        if archive_target.exists():
            shutil.rmtree(archive_target)
        shutil.move(str(path), str(archive_target))
        LOGGER.info("Archived prior cache directory %s -> %s", path.name, archive_target)


def get_ticker_cache_path(cache_dir: Path, ticker: str) -> Path:
    """Return the CSV cache path for a ticker."""
    return cache_dir / f"{ticker}.csv"


def normalize_history_frame(raw_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize vnstock history output to a stable schema."""
    normalized = raw_df.copy()
    column_map = {str(column).lower(): column for column in normalized.columns}

    if "time" not in column_map or "close" not in column_map:
        raise ValueError(f"{ticker}: missing required 'time' or 'close' columns.")

    rename_map = {
        column_map["time"]: "time",
        column_map["close"]: "close",
    }
    if "open" in column_map:
        rename_map[column_map["open"]] = "open"
    if "high" in column_map:
        rename_map[column_map["high"]] = "high"
    if "low" in column_map:
        rename_map[column_map["low"]] = "low"
    if "volume" in column_map:
        rename_map[column_map["volume"]] = "volume"

    normalized = normalized.rename(columns=rename_map)
    normalized["time"] = pd.to_datetime(normalized["time"]).dt.date
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")

    for optional_column in ("open", "high", "low", "volume"):
        if optional_column in normalized.columns:
            normalized[optional_column] = pd.to_numeric(
                normalized[optional_column], errors="coerce"
            )
        else:
            normalized[optional_column] = pd.NA

    normalized = normalized[
        ["time", "open", "high", "low", "close", "volume"]
    ].dropna(subset=["time", "close"])
    normalized = normalized.sort_values("time").drop_duplicates("time", keep="last")
    normalized["ticker"] = ticker
    normalized["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    return normalized.reset_index(drop=True)


def load_cached_ticker(cache_path: Path, ticker: str) -> pd.DataFrame:
    """Load a ticker from today's CSV cache."""
    df = pd.read_csv(cache_path)
    if df.empty:
        raise ValueError(f"{ticker}: cached CSV is empty.")

    df["time"] = pd.to_datetime(df["time"]).dt.date
    df["ticker"] = ticker
    return df


def save_ticker_to_cache(df: pd.DataFrame, cache_path: Path) -> None:
    """Persist a successful API response immediately to disk."""
    df.to_csv(cache_path, index=False, encoding="utf-8-sig")


def fetch_with_failover(
    ticker: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch a ticker from vnstock with internal source failover."""
    last_error: Exception | None = None
    rate_limited = False

    for source in API_SOURCES:
        try:
            stock = Vnstock().stock(symbol=ticker, source=source)
            raw_df = stock.quote.history(
                start=start_date,
                end=end_date,
                interval="1D",
            )

            if raw_df is None or raw_df.empty:
                raise ValueError(f"{ticker}: {source} returned no rows.")

            normalized = normalize_history_frame(raw_df, ticker)
            if normalized.empty:
                raise ValueError(f"{ticker}: {source} normalized dataset is empty.")

            normalized["source"] = source
            time.sleep(API_CALL_DELAY_SECONDS)
            return normalized
        except Exception as exc:
            last_error = exc
            error_text = str(exc).lower()
            if "429" in error_text or "rate limit" in error_text:
                rate_limited = True
                LOGGER.warning(
                    "%s hit explicit rate limit on %s: %s. Backing off for %s seconds.",
                    ticker,
                    source,
                    exc,
                    ERROR_BACKOFF_SECONDS,
                )
                time.sleep(ERROR_BACKOFF_SECONDS)
                break

            LOGGER.warning("%s failed on source %s: %s", ticker, source, exc)
            continue

    if not rate_limited:
        LOGGER.warning(
            "%s failed across all vnstock sources %s. Backing off for %s seconds.",
            ticker,
            API_SOURCES,
            ERROR_BACKOFF_SECONDS,
        )
        time.sleep(ERROR_BACKOFF_SECONDS)

    raise RuntimeError(f"{ticker} failed across all vnstock sources: {last_error}")


def fetch_with_retry(
    ticker: str,
    cache_dir: Path,
    start_date: str,
    end_date: str,
) -> FetchResult:
    """Fetch a ticker using today's cache first, then vnstock source failover."""
    cache_path = get_ticker_cache_path(cache_dir, ticker)
    if cache_path.exists():
        try:
            cached_df = load_cached_ticker(cache_path, ticker)
            LOGGER.info("%s loaded from cache", ticker)
            return FetchResult(
                ticker=ticker,
                status="cached",
                dataframe=cached_df,
                detail="Loaded from disk cache.",
            )
        except Exception as exc:
            LOGGER.warning("%s cache read failed: %s", ticker, exc)

    try:
        fetched_df = fetch_with_failover(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
        )
        save_ticker_to_cache(fetched_df, cache_path)
        source = fetched_df["source"].iloc[-1] if "source" in fetched_df.columns else "unknown"
        LOGGER.info(
            "%s fetched via vnstock source %s and saved to %s",
            ticker,
            source,
            cache_path.name,
        )
        return FetchResult(
            ticker=ticker,
            status="fetched",
            dataframe=fetched_df,
            detail=f"Fetched successfully via source {source}.",
        )
    except Exception as exc:
        LOGGER.error("%s failed after vnstock failover: %s", ticker, exc)
        return FetchResult(
            ticker=ticker,
            status="failed",
            dataframe=None,
            detail=str(exc),
        )


def compile_dataset(results: Iterable[FetchResult]) -> tuple[pd.DataFrame, list[str]]:
    """Combine all valid ticker datasets into one DataFrame."""
    valid_frames: list[pd.DataFrame] = []
    valid_tickers: list[str] = []

    for result in results:
        if result.dataframe is None or result.dataframe.empty:
            continue
        valid_frames.append(result.dataframe.copy())
        valid_tickers.append(result.ticker)

    if not valid_frames:
        return pd.DataFrame(), []

    combined = pd.concat(valid_frames, ignore_index=True)
    combined = combined.sort_values(["time", "ticker"]).reset_index(drop=True)
    return combined, valid_tickers


def main() -> None:
    """Run the end-of-day downloader."""
    configure_logging()
    setup_vnstock_api_key()
    archive_previous_day_cache()
    cache_dir = get_today_cache_dir()
    tickers = read_tickers(limit=100)

    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=FETCH_DAYS_BACK)).isoformat()

    LOGGER.info("Starting EOD batch download for %s tickers", len(tickers))
    LOGGER.info("Cache directory: %s", cache_dir)
    LOGGER.info("API sources (prioritized): %s", API_SOURCES)
    LOGGER.info("Fetch window: %s to %s", start_date, end_date)

    results: list[FetchResult] = []
    for index, ticker in enumerate(tickers, start=1):
        LOGGER.info("Processing %s/%s: %s", index, len(tickers), ticker)
        results.append(fetch_with_retry(ticker, cache_dir, start_date, end_date))

    combined_df, valid_tickers = compile_dataset(results)
    combined_path = cache_dir / "combined_dataset.csv"
    if not combined_df.empty:
        combined_df.to_csv(combined_path, index=False, encoding="utf-8-sig")
        LOGGER.info("Combined dataset saved to %s", combined_path)
    else:
        LOGGER.error("No valid ticker data was collected.")

    failed_tickers = [
        result.ticker for result in results if result.status == "failed"
    ]
    LOGGER.info("Valid tickers: %s", len(valid_tickers))
    LOGGER.info("Failed tickers: %s", len(failed_tickers))

    if len(valid_tickers) < MIN_VALID_TICKERS:
        LOGGER.critical(
            "Validation warning: only %s valid tickers collected. "
            "Market breadth calculation will be skewed.",
            len(valid_tickers),
        )

    if failed_tickers:
        LOGGER.warning("Failed symbols: %s", ", ".join(failed_tickers))


if __name__ == "__main__":
    main()
