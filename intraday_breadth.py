#!/usr/bin/env python3
"""Intraday breadth snapshot — runs every 15 min during VN trading hours.

Fetches current prices for top-100 tickers via vnstock Trading.price_board(),
computes % above SMA-3/5/10/20/50/200 using yesterday's combined_dataset.csv
(restored from GCS) for SMA history, appends today's tick to
intraday_breadth.json on GCS.

Trigger window (Asia/Ho_Chi_Minh, weekdays only):
  Morning   09:30 → 11:30
  Afternoon 13:00 → 14:45

Outside the window, the script logs and exits 0 (idempotent for cron).
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
TICKERS_FILE = SCRIPT_DIR / "tickers.csv"
ICT = ZoneInfo("Asia/Ho_Chi_Minh")

GCS_BUCKET = os.environ.get("INTRADAY_GCS_BUCKET", "vn-market-breadth")
GCS_COMBINED_KEY = "intraday/combined_dataset.csv"     # seeded by daily entrypoint
GCS_INTRADAY_KEY = "intraday_breadth.json"              # the live JSON the dashboard polls

MA_PERIODS = [3, 5, 10, 20, 50, 200]
MIN_OBS = 10  # EOD chart requires ≥10 daily observations per ticker (matches calculate_breadth)
PRICE_DIVISOR = 1000.0  # vnstock prices are in raw VND; combined_dataset.csv is in 'thousand VND'

# Trading-hour boundaries (ICT)
MORNING_START   = dtime(9, 30)
MORNING_END     = dtime(11, 30)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END   = dtime(14, 45)

LOGGER = logging.getLogger("intraday_breadth")


def configure_logging() -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    # Force UTF-8 stdout for Windows local runs
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%H:%M:%S")
    )
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def is_trading_window(now_ict: datetime) -> bool:
    if now_ict.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now_ict.time()
    if MORNING_START <= t <= MORNING_END:
        return True
    if AFTERNOON_START <= t <= AFTERNOON_END:
        return True
    return False


def get_breadth_universe(combined_path: Path) -> list[str]:
    """Return the same universe the EOD breadth chart uses.

    market_breadth.py:load_price_data_from_combined_dataset includes every
    ticker in combined_dataset.csv with >= 10 daily observations, excluding
    VNINDEX. We mirror that exactly so the intraday T-1 anchor matches the
    EOD chart's rightmost-1 column number-for-number.
    """
    df = pd.read_csv(combined_path, encoding="utf-8-sig")
    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df = df[df["ticker"] != "VNINDEX"]
    counts = df.groupby("ticker").size()
    return sorted(counts[counts >= MIN_OBS].index.tolist())


def fetch_current_prices(tickers: list[str]) -> dict[str, float]:
    """Single batch call via Trading.price_board(). Returns ticker -> price (in 'thousand VND')."""
    from vnstock import Trading
    trading = Trading(source="VCI")
    board = trading.price_board(tickers)
    if board is None or board.empty:
        raise RuntimeError("Trading.price_board() returned no rows")

    symbols = board[("listing", "symbol")].astype(str).str.upper().str.strip()
    match_prices = pd.to_numeric(board[("match", "match_price")], errors="coerce")
    ref_prices = pd.to_numeric(board[("listing", "ref_price")], errors="coerce")

    out: dict[str, float] = {}
    for symbol, match, ref in zip(symbols, match_prices, ref_prices):
        # Use match_price if active; fall back to ref_price (yesterday's reference) if no trades yet
        price = match if pd.notna(match) and match > 0 else ref
        if pd.notna(price) and price > 0:
            out[symbol] = float(price) / PRICE_DIVISOR
    return out


def download_combined_dataset(local_dst: Path) -> Path:
    """Pull the latest combined_dataset.csv from GCS into a local file."""
    from google.cloud import storage
    client = storage.Client()
    blob = client.bucket(GCS_BUCKET).blob(GCS_COMBINED_KEY)
    if not blob.exists():
        raise FileNotFoundError(
            f"gs://{GCS_BUCKET}/{GCS_COMBINED_KEY} not found. "
            "The daily pipeline must have uploaded it (entrypoint.sh)."
        )
    local_dst.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(local_dst))
    return local_dst


def _build_eod_prices_frame(combined_path: Path) -> pd.DataFrame:
    """Reproduce calculate_breadth()'s prices DataFrame: every ticker in
    combined_dataset.csv with >=10 obs, excluding VNINDEX, ffilled up to 2.
    """
    df = pd.read_csv(combined_path, encoding="utf-8-sig")
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time", "ticker", "close"])
    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["ticker"] != "VNINDEX"]

    frames = []
    for tkr, sub in df.groupby("ticker"):
        if len(sub) < MIN_OBS:
            continue
        s = sub.set_index("time")["close"]
        s = s[~s.index.duplicated(keep="last")]
        frames.append(s.rename(tkr))
    if not frames:
        return pd.DataFrame()

    prices = pd.concat(frames, axis=1).sort_index().ffill(limit=2)
    today_dt = pd.Timestamp(datetime.now(ICT).date())
    if today_dt in prices.index:
        prices = prices.drop(today_dt)
    return prices


def compute_breadth(combined_path: Path, current_prices: dict[str, float]) -> dict[str, float | int | None]:
    """% of universe with intraday_price > SMA-N(close[T-N+1..T-1]).

    Universe & SMA construction match the EOD chart's calculate_breadth()
    exactly: every ticker in combined_dataset.csv with >=10 daily observations,
    excluding VNINDEX. The SMA reference is frozen at T-1 (yesterday's close);
    only the price being compared changes between intraday ticks.
    """
    prices = _build_eod_prices_frame(combined_path)
    if prices.empty:
        return {f"mbz{p}": None for p in MA_PERIODS} | {"sample_size": 0}

    breadth: dict[str, float | int | None] = {}
    sample_size = 0
    for period in MA_PERIODS:
        sma = prices.rolling(period, min_periods=period).mean()
        latest_sma = sma.iloc[-1]  # SMA at T-1

        n_total = int(latest_sma.notna().sum())
        n_above = 0
        for ticker, sma_val in latest_sma.items():
            if pd.isna(sma_val):
                continue
            px = current_prices.get(ticker)
            if px is None or pd.isna(px):
                continue
            if px > sma_val:
                n_above += 1
        pct = round((n_above / n_total) * 100.0, 2) if n_total else None
        breadth[f"mbz{period}"] = pct
        sample_size = max(sample_size, n_total)

    breadth["sample_size"] = sample_size
    return breadth


def compute_t_minus_1_eod_breadth(combined_path: Path) -> tuple[dict, "datetime.date | None"]:
    """% above SMA-N at T-1 EOD: close[T-1] vs SMA built from N closes ending T-1.

    Numerically identical to the EOD chart's rightmost-1 column. Same
    universe, same SMA, just close[T-1] as the comparison price.
    """
    prices = _build_eod_prices_frame(combined_path)
    if prices.empty:
        return {f"mbz{p}": None for p in MA_PERIODS} | {"sample_size": 0}, None

    t_minus_1_date = prices.index.max().date()

    breadth: dict = {}
    sample_size = 0
    for period in MA_PERIODS:
        sma = prices.rolling(period, min_periods=period).mean()
        above = (prices > sma)
        n_above = int(above.iloc[-1].sum())
        n_total = int(sma.iloc[-1].notna().sum())
        pct = round((n_above / n_total) * 100.0, 2) if n_total else None
        breadth[f"mbz{period}"] = pct
        sample_size = max(sample_size, n_total)

    breadth["sample_size"] = sample_size
    return breadth, t_minus_1_date


def update_intraday_json_on_gcs(now_ict: datetime, breadth: dict, t_minus_1: dict | None) -> dict:
    """Read existing JSON from GCS, append today's tick, write back. Returns full doc."""
    from google.cloud import storage
    client = storage.Client()
    blob = client.bucket(GCS_BUCKET).blob(GCS_INTRADAY_KEY)
    today_str = now_ict.strftime("%Y-%m-%d")

    # Pull existing (may not exist yet, or be from a previous day)
    existing = None
    try:
        if blob.exists():
            existing = json.loads(blob.download_as_text())
    except Exception as exc:
        LOGGER.warning("Could not read existing %s: %s", GCS_INTRADAY_KEY, exc)

    if not existing or existing.get("date") != today_str:
        existing = {"date": today_str, "updates": []}

    tick = {
        "kind": "intraday",
        "time": now_ict.strftime("%H:%M"),
        "timestamp_ict": now_ict.strftime("%Y-%m-%d %H:%M:%S %z"),
        **{k: (None if v is None else v) for k, v in breadth.items()},
    }

    # Re-anchor: rebuild updates list = [T-1 EOD] + sorted(intraday ticks).
    # Old T-1 entries are dropped (we recompute fresh each tick); intraday
    # ticks are kept (deduped by HH:MM).
    by_time: dict[str, dict] = {}
    for u in existing["updates"]:
        if u.get("kind") == "eod_t_minus_1":
            continue  # we'll rebuild this from the freshly-computed t_minus_1
        by_time[u["time"]] = u
    by_time[tick["time"]] = tick
    sorted_intraday = [by_time[t] for t in sorted(by_time)]

    new_updates: list[dict] = []
    if t_minus_1 is not None:
        new_updates.append(t_minus_1)
    new_updates.extend(sorted_intraday)
    existing["updates"] = new_updates
    existing["last_updated_ict"] = now_ict.strftime("%H:%M %d/%m/%Y")

    blob.cache_control = "no-cache, no-store, must-revalidate"
    blob.upload_from_string(
        json.dumps(existing, ensure_ascii=False, indent=2),
        content_type="application/json",
    )
    return existing


def main() -> int:
    configure_logging()
    now_ict = datetime.now(ICT)
    LOGGER.info("Intraday breadth tick @ %s ICT", now_ict.strftime("%Y-%m-%d %H:%M:%S"))

    # Allow forcing past the time window for local testing.
    force = os.environ.get("INTRADAY_FORCE", "").lower() in ("1", "true", "yes")
    if not force and not is_trading_window(now_ict):
        LOGGER.info(
            "Outside trading window (09:30–11:30 / 13:00–14:45 ICT, weekdays). No-op."
        )
        return 0

    # Local override for testing: point INTRADAY_LOCAL_COMBINED at an existing combined_dataset.csv
    local_override = os.environ.get("INTRADAY_LOCAL_COMBINED")
    if local_override:
        combined_local = Path(local_override)
        if not combined_local.exists():
            raise FileNotFoundError(f"INTRADAY_LOCAL_COMBINED not found: {combined_local}")
        LOGGER.info("Using local combined_dataset: %s", combined_local)
    else:
        combined_local = SCRIPT_DIR / "data" / "_intraday_combined.csv"
        LOGGER.info("Downloading SMA history from gs://%s/%s ...", GCS_BUCKET, GCS_COMBINED_KEY)
        download_combined_dataset(combined_local)

    universe = get_breadth_universe(combined_local)
    LOGGER.info(
        "Universe: %d tickers from combined_dataset.csv (>=%d obs, excl VNINDEX) — matches EOD chart",
        len(universe), MIN_OBS,
    )

    LOGGER.info("Fetching current prices via Trading.price_board() ...")
    current = fetch_current_prices(universe)
    LOGGER.info("Got prices for %d/%d tickers", len(current), len(universe))
    if len(current) < len(universe) // 2:
        raise RuntimeError(
            f"Only {len(current)} of {len(universe)} prices fetched — refusing to update breadth"
        )

    breadth = compute_breadth(combined_local, current)
    LOGGER.info(
        "Intraday: mbz3=%s mbz5=%s mbz10=%s mbz20=%s mbz50=%s mbz200=%s n=%s",
        breadth.get("mbz3"), breadth.get("mbz5"), breadth.get("mbz10"),
        breadth.get("mbz20"), breadth.get("mbz50"), breadth.get("mbz200"),
        breadth.get("sample_size"),
    )

    t_minus_1_breadth, t_minus_1_date = compute_t_minus_1_eod_breadth(combined_local)
    t_minus_1_entry: dict | None = None
    if t_minus_1_date is not None:
        LOGGER.info(
            "T-1 EOD (%s): mbz3=%s mbz5=%s mbz10=%s mbz20=%s mbz50=%s mbz200=%s n=%s",
            t_minus_1_date.strftime("%d/%m/%Y"),
            t_minus_1_breadth.get("mbz3"), t_minus_1_breadth.get("mbz5"),
            t_minus_1_breadth.get("mbz10"), t_minus_1_breadth.get("mbz20"),
            t_minus_1_breadth.get("mbz50"), t_minus_1_breadth.get("mbz200"),
            t_minus_1_breadth.get("sample_size"),
        )
        t_minus_1_entry = {
            "kind": "eod_t_minus_1",
            "time": f"Đóng T-1 ({t_minus_1_date.strftime('%d/%m')})",
            "date": t_minus_1_date.isoformat(),
            **{k: (None if v is None else v) for k, v in t_minus_1_breadth.items()},
        }

    if os.environ.get("INTRADAY_DRY_RUN", "").lower() in ("1", "true", "yes"):
        LOGGER.info("DRY_RUN — skipping GCS upload")
        LOGGER.info("Would write intraday tick: %s", json.dumps(breadth, ensure_ascii=False))
        if t_minus_1_entry:
            LOGGER.info("Would write T-1 EOD anchor: %s", json.dumps(t_minus_1_entry, ensure_ascii=False))
        return 0

    doc = update_intraday_json_on_gcs(now_ict, breadth, t_minus_1_entry)
    LOGGER.info(
        "Updated gs://%s/%s — %d ticks today (%s)",
        GCS_BUCKET, GCS_INTRADAY_KEY, len(doc["updates"]), doc["date"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
