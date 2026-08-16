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
from collections import Counter
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
RS_UNIVERSE_FILE = SCRIPT_DIR / "rs_fixed_tickers.csv"  # unified canonical universe
DATA_DIR = SCRIPT_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
ICT = ZoneInfo("Asia/Ho_Chi_Minh")

API_SOURCES = ["KBS", "VCI", "MSN", "FMP"]  # vnstock 3.5.0+ dropped SSI/VND; community-tier limit is 60 req/min so 1.1s spacing fits
API_CALL_DELAY_SECONDS = 1.1
ERROR_BACKOFF_SECONDS = 60
FETCH_DAYS_BACK = 420
MIN_VALID_TICKERS = 95
MIN_BARS_FOR_200D_MA = 200  # below this, market_breadth.py's 200-day MA is undefined
INDEX_TICKER = "VNINDEX"
# B2 scale-sanity band: reject a fetch only on a gross (~1000x) scale error
# (raw-VND MSN/FMP fallback, a vnstock scale change on VNINDEX) -- never on a
# normal daily move. False positives here drop a real ticker for the day.
SCALE_SANITY_MAX_RATIO = 100.0
SCALE_SANITY_MIN_RATIO = 0.01
# VN market closes 14:45 ICT; daily bars settle by ~15:00. A cache entry written
# before this hour cannot contain today's close. Mirrors market_breadth.py's
# freshness_cutoff -- keep the two in step.
POST_CLOSE_SETTLE_HOUR = 15

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


def read_rs_universe_tickers() -> list[str]:
    """Read the RS monitor universe (200 tickers) used by pre_breakout.py.

    Returns [] if the file is missing — the caller decides whether that's fatal.
    """
    if not RS_UNIVERSE_FILE.exists():
        LOGGER.warning(
            "Unified universe file not found at %s; pre-breakout coverage will be limited "
            "to the tickers.csv top-100 overlap.",
            RS_UNIVERSE_FILE,
        )
        return []

    df = pd.read_csv(RS_UNIVERSE_FILE)
    if "ticker" not in df.columns:
        LOGGER.warning("%s missing 'ticker' column; skipping.", RS_UNIVERSE_FILE.name)
        return []

    tickers = df["ticker"].dropna().astype(str).str.strip().str.upper()
    tickers = [t for t in tickers if t and t.lower() != "nan"]
    return tickers


def build_fetch_universe() -> list[str]:
    """Build the EOD fetch universe from rs_fixed_tickers.csv (+ VNINDEX).

    rs_fixed_tickers.csv is the unified canonical universe and the source of
    truth for what pre_breakout.py and rs_matrix_3T.py analyse. Driving the
    downloader off this file ensures OHLC coverage for every ticker that the
    matrix and pre-breakout layers depend on.
    """
    rs = read_rs_universe_tickers()
    if not rs:
        raise RuntimeError(
            f"{RS_UNIVERSE_FILE.name} is empty or unreadable; cannot build fetch universe."
        )

    seen: set[str] = set()
    ordered: list[str] = []
    for t in rs:
        t_upper = t.upper()
        if t_upper in seen:
            continue
        seen.add(t_upper)
        ordered.append(t_upper)

    if INDEX_TICKER not in seen:
        ordered.append(INDEX_TICKER)

    LOGGER.info(
        "Fetch universe: %d tickers from %s (+ %s)",
        len(rs),
        RS_UNIVERSE_FILE.name,
        INDEX_TICKER,
    )
    return ordered


def get_today_cache_dir() -> Path:
    """Return today's cache directory, creating it if needed."""
    # Explicit ICT rather than date.today(): archive_previous_day_cache() and
    # market_breadth.get_today_combined_dataset_path() both key on ICT, and a
    # naive clock silently disagrees with them whenever TZ is not set.
    today_dir = DATA_DIR / datetime.now(ICT).date().isoformat()
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


def _drop_degenerate_ohlc_rows(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Drop OHLC rows that are structurally impossible, before they are cached.

    dropna(subset=["time", "close"]) alone lets through a halted ticker's
    close=0 (0/1000 survives dropna), high<low, or a close outside [low, high]
    -- any of which goes false-bearish in market breadth. Open/high/low/volume
    may be NA (some feeds omit them), so each check only applies where its
    inputs are present.
    """
    if df.empty:
        return df

    bad = df["close"] <= 0

    has_hl = df["high"].notna() & df["low"].notna()
    bad |= has_hl & (df["high"] < df["low"])
    bad |= has_hl & (df["close"] < df["low"])
    bad |= has_hl & (df["close"] > df["high"])

    has_open = df["open"].notna()
    bad |= has_open & (df["open"] <= 0)
    bad |= has_open & has_hl & ((df["open"] < df["low"]) | (df["open"] > df["high"]))

    bad |= df["volume"].notna() & (df["volume"] < 0)

    dropped = int(bad.sum())
    if dropped:
        LOGGER.warning(
            "%s: dropped %s row(s) with impossible OHLC (close<=0, high<low, "
            "or close/open outside [low,high])",
            ticker, dropped,
        )
    return df[~bad].reset_index(drop=True)


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
    normalized = _drop_degenerate_ohlc_rows(normalized, ticker)
    normalized = normalized.sort_values("time").drop_duplicates("time", keep="last")
    normalized["ticker"] = ticker
    normalized["fetched_at"] = datetime.now(ICT).isoformat(timespec="seconds")
    return normalized.reset_index(drop=True)


def cache_entry_is_current(
    cached_df: pd.DataFrame,
    end_date: str,
    now_ict: datetime,
) -> bool:
    """Decide whether a same-day cache entry may be reused instead of refetched.

    The cache directory is keyed by ICT calendar date, so the 07:30 ICT run and
    the 15:15 ICT EOD run of the same weekday share it. The 07:30 snapshot
    legitimately stops at T-1 (VN has not traded yet), so reusing it after the
    close republishes yesterday's closes as today's -- the stale-dashboard bug.

    Pre-close runs may reuse any same-day entry. Post-close runs may only reuse
    an entry that already carries today's bar; anything older is refetched. That
    keeps same-slot retries cheap (a 15:40 rerun reuses the 15:15 fetch) without
    ever letting a pre-close snapshot survive into a post-close publish. On a VN
    holiday no entry ever carries today's bar, so a post-close run simply
    refetches and gets the same last-trading-day data back -- correct, just not
    free. Deliberately no calendar lookup: the repo has no holiday data, and
    guessing wrong here would silently republish stale closes.
    """
    if now_ict.hour < POST_CLOSE_SETTLE_HOUR:
        return True
    if cached_df.empty or "time" not in cached_df.columns:
        return False
    latest_bar = max(pd.to_datetime(cached_df["time"]).dt.date)
    return latest_bar >= date.fromisoformat(end_date)


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


def _fetch_ssi_daily(ticker: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """Fetch one ticker's daily OHLCV from SSI FastConnect.

    SSI's `daily_ohlc` returns RAW VND, so OHLC prices are divided by 1000 to
    match combined_dataset.csv's thousand-VND scale (volume stays raw shares).
    Returns a frame in the same schema as `normalize_history_frame` (minus the
    `source` column, which the caller sets), or None if SSI has no rows for the
    symbol — e.g. VNINDEX (an index, not a security) is not served by SSI's
    security `daily_ohlc` and falls back to vnstock.
    """
    import ssi_client

    s = date.fromisoformat(start_date)
    e = date.fromisoformat(end_date)
    raw = ssi_client.get_daily_ohlcv(ticker, s, e)
    if raw is None or raw.empty:
        return None

    out = pd.DataFrame({
        "time": pd.to_datetime(raw["ts"]).dt.date,
        "open": pd.to_numeric(raw["open"], errors="coerce") / 1000.0,
        "high": pd.to_numeric(raw["high"], errors="coerce") / 1000.0,
        "low": pd.to_numeric(raw["low"], errors="coerce") / 1000.0,
        "close": pd.to_numeric(raw["close"], errors="coerce") / 1000.0,
        "volume": pd.to_numeric(raw["volume"], errors="coerce"),
    })
    out = out.dropna(subset=["time", "close"])
    out = _drop_degenerate_ohlc_rows(out, ticker)
    out = out.sort_values("time").drop_duplicates("time", keep="last")
    if out.empty:
        return None
    out["ticker"] = ticker
    out["fetched_at"] = datetime.now(ICT).isoformat(timespec="seconds")
    return out.reset_index(drop=True)


def fetch_with_failover(
    ticker: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch a ticker: SSI FastConnect primary, vnstock source failover fallback.

    SSI replaced vnstock as the primary daily source on 2026-06-22 (vnstock's
    live `price_board` began returning HTTP 403 from the cloud; `quote.history`
    still works but is degrading). vnstock's `quote.history` is kept as a
    fallback — it still works and, crucially, serves VNINDEX, which SSI's
    security `daily_ohlc` endpoint does not return.
    """
    # --- SSI FastConnect first (self rate-limited inside ssi_client) ---------
    try:
        ssi_df = _fetch_ssi_daily(ticker, start_date, end_date)
        if ssi_df is not None and not ssi_df.empty:
            ssi_df["source"] = "SSI"
            return ssi_df
        LOGGER.info("%s: SSI returned no rows; falling back to vnstock", ticker)
    except Exception as exc:
        LOGGER.warning("%s: SSI daily fetch failed (%s); falling back to vnstock", ticker, exc)

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


def _reference_median_close(
    ticker: str, cache_dir: Path, cached_df: pd.DataFrame | None
) -> float | None:
    """Best-effort prior median close for `ticker`, for the B2 scale check only.

    Prefers today's already-loaded cache entry (a same-day rerun); otherwise
    falls back to the most recent archived day that still has this ticker.
    Returns None when neither exists (e.g. a brand-new ticker), so the caller
    skips the check rather than guessing at a reference.
    """
    if cached_df is not None and not cached_df.empty:
        return float(cached_df["close"].median())

    if not ARCHIVE_DIR.exists():
        return None
    for day_dir in sorted((p for p in ARCHIVE_DIR.iterdir() if p.is_dir()), reverse=True):
        candidate_path = get_ticker_cache_path(day_dir, ticker)
        if not candidate_path.exists():
            continue
        try:
            archived_df = load_cached_ticker(candidate_path, ticker)
        except Exception:
            continue
        if not archived_df.empty:
            return float(archived_df["close"].median())
    return None


def _scale_is_sane(fetched_df: pd.DataFrame, ticker: str, reference_median: float | None) -> bool:
    """Reject a frame whose median close is ~1000x off a trusted prior median.

    Catches a raw-VND MSN/FMP fallback (1000x too high) or a stray vnstock
    scale change (e.g. on VNINDEX) without flagging normal daily moves -- the
    band is deliberately wide because a false positive here drops a real
    ticker for the day. No reference (new ticker, nothing archived yet) means
    skip: never reject on a guess.
    """
    if reference_median is None or reference_median <= 0:
        return True
    if fetched_df.empty:
        return True

    fetched_median = float(fetched_df["close"].median())
    if fetched_median <= 0:
        return True  # degenerate frame; B1 already stripped close<=0 rows

    ratio = fetched_median / reference_median
    if ratio > SCALE_SANITY_MAX_RATIO or ratio < SCALE_SANITY_MIN_RATIO:
        LOGGER.warning(
            "%s: fetched median close %.4f is %.1fx the prior reference %.4f -- "
            "rejecting as a likely scale error.",
            ticker, fetched_median, ratio, reference_median,
        )
        return False
    return True


def fetch_with_retry(
    ticker: str,
    cache_dir: Path,
    start_date: str,
    end_date: str,
    now_ict: datetime,
) -> FetchResult:
    """Fetch a ticker using today's cache first, then vnstock source failover."""
    cache_path = get_ticker_cache_path(cache_dir, ticker)
    cached_df: pd.DataFrame | None = None
    if cache_path.exists():
        try:
            cached_df = load_cached_ticker(cache_path, ticker)
            if cache_entry_is_current(cached_df, end_date, now_ict):
                LOGGER.info("%s loaded from cache", ticker)
                return FetchResult(
                    ticker=ticker,
                    status="cached",
                    dataframe=cached_df,
                    detail="Loaded from disk cache.",
                )
            stale_bar = max(pd.to_datetime(cached_df["time"]).dt.date)
            LOGGER.info(
                "%s cache stops at %s (pre-close snapshot); refetching for %s",
                ticker,
                stale_bar,
                end_date,
            )
        except Exception as exc:
            LOGGER.warning("%s cache read failed: %s", ticker, exc)
            cached_df = None

    try:
        fetched_df = fetch_with_failover(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
        )
        reference_median = _reference_median_close(ticker, cache_dir, cached_df)
        if not _scale_is_sane(fetched_df, ticker, reference_median):
            raise RuntimeError(
                f"{ticker}: fetched frame failed the scale sanity check "
                f"(median close vs. prior reference {reference_median:.4f})."
            )
        save_ticker_to_cache(fetched_df, cache_path)
        source = fetched_df["source"].iloc[-1] if "source" in fetched_df.columns else "unknown"
        LOGGER.info(
            "%s fetched via %s and saved to %s",
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
        # Refetch failed. Prefer the rejected pre-close entry over dropping the
        # ticker: its 420-day history is still needed for the moving averages,
        # and losing tickers outright would shrink the universe below
        # MIN_SUCCESSFUL_TICKERS and abort an otherwise publishable run. The
        # rows are honestly stale, so it is the freshness guard's coverage
        # check -- not this function -- that decides whether the run publishes.
        if cached_df is not None and not cached_df.empty:
            LOGGER.warning(
                "%s refetch failed (%s); falling back to its pre-close cache entry "
                "which stops at %s.",
                ticker,
                exc,
                max(pd.to_datetime(cached_df["time"]).dt.date),
            )
            return FetchResult(
                ticker=ticker,
                status="stale_cache",
                dataframe=cached_df,
                detail=f"Refetch failed; reused pre-close cache entry: {exc}",
            )
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
    thin_tickers: list[str] = []

    for result in results:
        if result.dataframe is None or result.dataframe.empty:
            continue
        valid_frames.append(result.dataframe.copy())
        valid_tickers.append(result.ticker)
        if len(result.dataframe) < MIN_BARS_FOR_200D_MA:
            thin_tickers.append(result.ticker)

    if thin_tickers:
        LOGGER.warning(
            "%s ticker(s) have < %s bars (200-day MA undefined): %s",
            len(thin_tickers), MIN_BARS_FOR_200D_MA, ", ".join(thin_tickers),
        )

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
    tickers = build_fetch_universe()

    # One clock for the whole sweep: the run spans ~6 minutes, and letting each
    # ticker re-read the time would flip the cache rule mid-run at 15:00.
    now_ict = datetime.now(ICT)
    end_date = now_ict.date().isoformat()
    start_date = (now_ict.date() - timedelta(days=FETCH_DAYS_BACK)).isoformat()

    LOGGER.info("Starting EOD batch download for %s tickers", len(tickers))
    LOGGER.info("Cache directory: %s", cache_dir)
    LOGGER.info("API sources (prioritized): %s", API_SOURCES)
    LOGGER.info("Fetch window: %s to %s", start_date, end_date)
    LOGGER.info(
        "Run slot: %s ICT (%s) -- cache entries without a %s bar %s",
        now_ict.strftime("%H:%M"),
        "post-close" if now_ict.hour >= POST_CLOSE_SETTLE_HOUR else "pre-close",
        end_date,
        "are refetched" if now_ict.hour >= POST_CLOSE_SETTLE_HOUR else "are reused as-is",
    )

    results: list[FetchResult] = []
    for index, ticker in enumerate(tickers, start=1):
        LOGGER.info("Processing %s/%s: %s", index, len(tickers), ticker)
        results.append(fetch_with_retry(ticker, cache_dir, start_date, end_date, now_ict))

    combined_df, valid_tickers = compile_dataset(results)
    combined_path = cache_dir / "combined_dataset.csv"
    if not combined_df.empty:
        combined_df.to_csv(combined_path, index=False, encoding="utf-8-sig")
        LOGGER.info("Combined dataset saved to %s", combined_path)
        # Scale + source sanity: SSI primary must land in the same thousand-VND
        # scale as the vnstock fallback (FPT ~70.x, VNINDEX ~1.8x).
        if "source" in combined_df.columns:
            src_counts = combined_df.groupby("source")["ticker"].nunique().to_dict()
            LOGGER.info("Source coverage (unique tickers per source): %s", src_counts)
        for probe in ("FPT", "VNINDEX"):
            sub = combined_df[combined_df["ticker"] == probe]
            if not sub.empty:
                last = sub.sort_values("time").iloc[-1]
                LOGGER.info(
                    "PROBE %s: last close=%.4f source=%s time=%s",
                    probe, float(last["close"]), last.get("source", "?"), last["time"],
                )
    else:
        LOGGER.error("No valid ticker data was collected.")

    failed_tickers = [
        result.ticker for result in results if result.status == "failed"
    ]
    LOGGER.info("Valid tickers: %s", len(valid_tickers))
    LOGGER.info("Failed tickers: %s", len(failed_tickers))

    # A 100%-cached run looks identical to a healthy one in every other log line
    # (load_cached_ticker re-emits the original fetched_at and source), which is
    # how the stale dashboard went unnoticed. State the split and the newest bar
    # outright so "did this run actually fetch?" is answerable from the log.
    status_counts = Counter(result.status for result in results)
    newest_bar = combined_df["time"].max() if not combined_df.empty else "n/a"
    LOGGER.info(
        "Run summary | fetched=%s cached=%s stale_cache=%s failed=%s | newest bar in dataset=%s",
        status_counts.get("fetched", 0),
        status_counts.get("cached", 0),
        status_counts.get("stale_cache", 0),
        status_counts.get("failed", 0),
        newest_bar,
    )
    if status_counts.get("stale_cache"):
        LOGGER.warning(
            "%s ticker(s) fell back to a pre-close cache entry after a failed refetch; "
            "their latest bar is older than %s.",
            status_counts["stale_cache"],
            end_date,
        )

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
