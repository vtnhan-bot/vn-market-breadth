"""dnse_client.py — DNSE EntradeX chart-api daily-OHLC client (PRIMARY feed).

Unauthenticated historical OHLC from the EntradeX chart-api
(``services.entrade.com.vn/chart-api``), the SAME data DNSE serves through its
signed OpenAPI but reachable from the GCP VM with no key. It became the pipeline's
primary daily source on 2026-08-21 because it carries **no settlement lag**: it
returns T's daily bar within minutes of the 14:45 ICT close, whereas SSI's
``daily_ohlc`` settles the official bar ~10h later — which silently dropped
same-day rows for names like DHC/DGW when the 15:15 run fetched before settlement.

Scale returned here is **DNSE-native**, left for each caller's ``_fetch_dnse_daily``
to normalize to the pipeline scale:
  * stocks  -> thousand-VND (e.g. DHC 36.55) — already the pipeline's stock scale.
  * indices -> raw points   (e.g. VNINDEX 1734.24) — the caller divides by 1000 to
               reach combined_dataset.csv's thousand-points VNINDEX convention
               (market_breadth.py stores/plots VNINDEX as 1.90 == 1,900).
Volume is raw shares (matches SSI + vnstock).

This module is a pure transport (``requests`` only, no auth): an EMPTY window
returns ``None`` (a genuinely non-trading window — the caller falls through to
SSI); a transport failure RAISES (the caller's try/except falls through to SSI).
That empty-vs-down discipline mirrors the SSI-primary pattern it fronts.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

log = logging.getLogger(__name__)

CHART_BASE = "https://services.entrade.com.vn/chart-api/v2/ohlcs"
_ICT = ZoneInfo("Asia/Ho_Chi_Minh")
# (connect, read): fail a dead host fast, but allow a slow full-history read.
_TIMEOUT = (5.0, 20.0)
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.5

# Symbols priced in index POINTS (not thousand-VND). The chart-api serves these
# from a different path segment (``/index``) and they must NOT be treated as
# stocks. Kept in step with market_breadth.INDEX_TICKER (VNINDEX) plus the other
# HOSE/HNX indices in case the universe ever grows.
_INDEX_SYMBOLS = {
    "VNINDEX", "VN30", "VN100", "VNALLSHARE", "VNXALL", "VNX50",
    "HNX", "HNXINDEX", "HNX30", "UPCOM", "UPINDEX", "UPCOMINDEX",
}

_COLS = ["time", "open", "high", "low", "close", "volume"]


def is_index(symbol: str) -> bool:
    """True when `symbol` is a market index (points-scaled, /index endpoint)."""
    return symbol.upper() in _INDEX_SYMBOLS


def _segment(symbol: str) -> str:
    return "index" if is_index(symbol) else "stock"


def _ict_midnight_unix(d: date) -> int:
    """Unix seconds at 00:00 ICT on date `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=_ICT).timestamp())


def _get_json(seg: str, params: dict) -> dict:
    """GET the chart-api with a short retry on transport hiccups.

    A transient network blip must not cascade the fetch down to the laggy SSI
    fallback (the very lag DNSE exists to avoid), so retry the transport a couple
    of times before giving up and letting the exception propagate.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(f"{CHART_BASE}/{seg}", params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json() or {}
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                log.debug("DNSE chart-api %s attempt %d/%d failed: %s; retrying",
                          params.get("symbol"), attempt, _MAX_ATTEMPTS, exc)
                time.sleep(_RETRY_BACKOFF_SECONDS)
    # Exhausted retries: raise so the caller falls back to SSI (down != empty).
    raise last_exc  # type: ignore[misc]


def get_daily_ohlcv(symbol: str, start: date, end: date) -> pd.DataFrame | None:
    """Daily OHLCV inclusive ``[start, end]`` from the chart-api, DNSE-native scale.

    Returns a DataFrame ``[time(date), open, high, low, close, volume]`` sorted
    ascending by day, or ``None`` when the window is genuinely empty (non-trading
    range, unknown symbol). Raises on a transport failure so the caller's
    try/except can fall back to SSI — an empty answer is NOT an error.
    """
    sym = symbol.upper()
    seg = _segment(sym)
    frm = _ict_midnight_unix(start)
    to = _ict_midnight_unix(end) + 86400  # make `end` inclusive
    data = _get_json(seg, {
        "symbol": sym,
        "resolution": "1D",
        "from": frm,
        "to": to,
    })
    t = data.get("t") or []
    if not t:
        return None
    # The chart-api stamps each daily bar during the ICT session; convert the
    # epoch through UTC to ICT before taking the calendar date so the bar lands
    # on the correct trading day regardless of the exact intraday stamp.
    ts = (
        pd.to_datetime(pd.Series(t, dtype="int64"), unit="s", utc=True)
        .dt.tz_convert(_ICT)
        .dt.date
    )
    df = pd.DataFrame({
        "time": ts,
        "open": pd.to_numeric(pd.Series(data.get("o") or []), errors="coerce"),
        "high": pd.to_numeric(pd.Series(data.get("h") or []), errors="coerce"),
        "low": pd.to_numeric(pd.Series(data.get("l") or []), errors="coerce"),
        "close": pd.to_numeric(pd.Series(data.get("c") or []), errors="coerce"),
        "volume": pd.to_numeric(pd.Series(data.get("v") or []), errors="coerce"),
    })
    df = df.dropna(subset=["time", "close"]).sort_values("time").reset_index(drop=True)
    if df.empty:
        return None
    return df[_COLS]
