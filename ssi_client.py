#!/usr/bin/env python3
"""ssi_client.py — minimal SSI FastConnect Data REST client for the breadth engine.

Why this exists
---------------
vnstock's `Trading.price_board()` (the old live-price source for the intraday
breadth snapshot) started returning HTTP 403. SSI FastConnect is the proven,
GCP-reachable replacement already used by `vn-trading-signal`. This module is a
self-contained, trimmed adaptation of that engine's `feed/api_client.py`
(`SSIFastConnectClient`) — only the pieces the breadth engine needs:

    get_current_prices(tickers) -> {TICKER_UPPER: price_in_thousand_VND}
    get_daily_ohlcv(symbol, start, end) -> canonical OHLCV DataFrame

Critical SSI facts baked in here
--------------------------------
* SSI's REST API has **no tick/quote/price-board endpoint**. The only way to get
  "current price" over REST is the last bar of `intraday_ohlc(resolution=1)`
  (last 1-minute bar close = "now"). That means **one REST call per symbol**.
* SSI enforces ~1 request/sec. We serialize every SDK call through a token-bucket
  rate limiter (default 0.9 req/sec for clock-skew margin) and retry on 429.
* All REST date params are **DD/MM/YYYY**.
* SSI returns the HTTP status inside the JSON body (`{"status": ...}`); an empty
  `data: []` with a success status means "no data", not an error.
* Credentials come from env vars only — never hardcoded. We accept the long
  names `SSI_FC_DATA_CONSUMER_ID` / `SSI_FC_DATA_CONSUMER_SECRET` and the short
  names `ID` / `secret` (what SSI's own `.env` uses).

Timing note for ~100 symbols
-----------------------------
At 0.9 req/sec a cold full sweep of 100 last-minute-bar fetches is ~111 s
(~1.9 min). That fits comfortably inside a 15-minute intraday poll cycle. Symbols
with no bar yet (pre-open / halted) are skipped, not fatal.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

LOGGER = logging.getLogger("ssi_client")

ICT = ZoneInfo("Asia/Ho_Chi_Minh")

# SSI FastConnect Data defaults (from SSI docs / vn-trading-signal settings.yaml).
DEFAULT_REST_URL = "https://fc-data.ssi.com.vn/"
DEFAULT_STREAM_URL = "https://fc-datahub.ssi.com.vn/"
DEFAULT_AUTH_TYPE = "Bearer"

# Rate/retry tuning. SSI enforces ~1 req/sec; 0.9 leaves clock-skew margin.
DEFAULT_RATE_LIMIT_PER_SEC = 0.9
DEFAULT_MAX_RETRIES = 4
DEFAULT_RETRY_BACKOFF_SEC = 0.5

# Breadth/RS expect prices in 'thousand VND'; SSI returns raw VND.
PRICE_DIVISOR = 1000.0

REQUIRED_OHLCV_COLS = ["ts", "symbol", "open", "high", "low", "close", "volume", "value"]


def _read_credentials() -> tuple[Optional[str], Optional[str]]:
    """Resolve (consumer_id, consumer_secret) from the environment.

    Prefers the long canonical names, falls back to SSI's short `.env` names.
    Returns (None, None) components individually if absent — caller validates.
    """
    cid = os.environ.get("SSI_FC_DATA_CONSUMER_ID") or os.environ.get("ID")
    sec = os.environ.get("SSI_FC_DATA_CONSUMER_SECRET") or os.environ.get("secret")
    return (cid or None), (sec or None)


class _RateLimiter:
    """Token-bucket limiter — serializes SDK calls to <= per_sec requests/sec."""

    def __init__(self, per_sec: float):
        self._interval = 1.0 / max(per_sec, 0.1)
        self._next_ok = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_ok:
            time.sleep(self._next_ok - now)
        self._next_ok = max(now, self._next_ok) + self._interval


class _SDKConfigShim:
    """Adapter: the SSI SDK reads `consumerID`, `consumerSecret`, `auth_type`,
    `url`, `stream_url` as flat attributes. We present them in that shape and
    force a trailing slash on both URLs (the SDK requires it)."""

    def __init__(self, consumer_id: str, consumer_secret: str,
                 rest_url: str, stream_url: str, auth_type: str):
        self.consumerID = consumer_id or ""
        self.consumerSecret = consumer_secret or ""
        self.auth_type = auth_type
        self.url = rest_url if rest_url.endswith("/") else rest_url + "/"
        self.stream_url = stream_url if stream_url.endswith("/") else stream_url + "/"


class SSIClient:
    """Minimal SSI FastConnect Data REST client for the breadth engine.

    Wraps the official `ssi_fc_data` SDK (imported lazily so importing this
    module never requires the SDK to be installed). All SDK calls go through a
    rate limiter + 429-aware retry, since current-price needs one call/symbol.
    """

    def __init__(
        self,
        consumer_id: Optional[str] = None,
        consumer_secret: Optional[str] = None,
        rest_url: str = DEFAULT_REST_URL,
        stream_url: str = DEFAULT_STREAM_URL,
        auth_type: str = DEFAULT_AUTH_TYPE,
        rate_limit_per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_sec: float = DEFAULT_RETRY_BACKOFF_SEC,
    ):
        if consumer_id is None or consumer_secret is None:
            env_cid, env_sec = _read_credentials()
            consumer_id = consumer_id or env_cid
            consumer_secret = consumer_secret or env_sec
        if not (consumer_id and consumer_secret):
            raise RuntimeError(
                "SSI credentials missing: set SSI_FC_DATA_CONSUMER_ID and "
                "SSI_FC_DATA_CONSUMER_SECRET (or ID / secret) in the environment."
            )

        self._max_retries = max_retries
        self._retry_backoff_sec = retry_backoff_sec
        self._rl = _RateLimiter(rate_limit_per_sec)

        # Lazy SDK import — keeps this module importable without ssi_fc_data
        # (e.g. unit tests, EOD-only runs).
        from ssi_fc_data import fc_md_client, model  # noqa: F401

        self._sdk_cfg = _SDKConfigShim(
            consumer_id, consumer_secret, rest_url, stream_url, auth_type
        )
        self._sdk = fc_md_client.MarketDataClient(self._sdk_cfg)
        self._model = model

    # -- SDK call wrapper: rate-limit + retry on 429/transient --------------
    def _sdk_call_with_retry(self, *, op: str, req) -> dict:
        """Call self._sdk.<op>(cfg, req) with rate limiting + retry.

        SSI puts the status in the JSON body. We treat 200/"Success" as OK,
        retry on 429 / "quota" / "rate" with exponential backoff, and return
        the (possibly error) payload otherwise so the caller can decide.
        """
        method = getattr(self._sdk, op)
        attempt = 0
        while True:
            self._rl.wait()
            resp = method(self._sdk_cfg, req) or {}
            status = resp.get("status")
            if status in (200, "200", "Success", "success"):
                return resp
            msg = str(resp.get("message", ""))[:120]
            if status in (429, "429") or "quota" in msg.lower() or "rate" in msg.lower():
                attempt += 1
                if attempt > self._max_retries:
                    LOGGER.warning(
                        "SSI %s status=%s after %d retries; giving up: %s",
                        op, status, attempt, msg,
                    )
                    return resp
                sleep = self._retry_backoff_sec * (2 ** (attempt - 1))
                LOGGER.info(
                    "SSI %s rate-limited (status=%s); retry %d in %.1fs",
                    op, status, attempt, sleep,
                )
                time.sleep(sleep)
                continue
            if status not in (None, 200, "200"):
                LOGGER.debug("SSI %s status=%s msg=%s", op, status, msg)
            return resp

    # -- intraday 1m bars for one symbol (best REST "current price" source) --
    def get_intraday_bars(
        self, symbol: str, trade_date: date, resolution_minutes: int = 1
    ) -> pd.DataFrame:
        """Return one trading day's OHLCV bars for `symbol`, ascending by ts.

        Last row's `close` is the best REST proxy for the current price.
        Returns an empty DataFrame (canonical columns) when no bar exists yet.
        """
        rows: list[dict] = []
        page = 1
        while True:
            resp = self._sdk_call_with_retry(
                op="intraday_ohlc",
                req=self._model.intraday_ohlc(
                    symbol=symbol.upper(),
                    fromDate=trade_date.strftime("%d/%m/%Y"),
                    toDate=trade_date.strftime("%d/%m/%Y"),
                    pageIndex=page,
                    pageSize=1000,
                    ascending=True,
                    resolution=int(resolution_minutes),
                ),
            )
            chunk = (resp or {}).get("data") or []
            if not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < 1000:
                break
            page += 1

        if not rows:
            return pd.DataFrame(columns=REQUIRED_OHLCV_COLS)

        df = pd.DataFrame(rows)
        rename = {
            "Symbol": "symbol", "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume", "Value": "value",
            "TradingDate": "trading_date", "Time": "_time",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "trading_date" in df.columns and "_time" in df.columns:
            ts_str = (
                df["trading_date"].astype(str).str.strip()
                + " "
                + df["_time"].astype(str).str.strip()
            )
            df["ts"] = pd.to_datetime(ts_str, format="%d/%m/%Y %H:%M:%S", errors="coerce")
        elif "_time" in df.columns:
            base = trade_date.strftime("%Y-%m-%d")
            df["ts"] = pd.to_datetime(base + " " + df["_time"].astype(str), errors="coerce")
        else:
            LOGGER.warning("intraday_ohlc rows missing time field; rows=%d", len(df))
            return pd.DataFrame(columns=REQUIRED_OHLCV_COLS)
        df["symbol"] = symbol.upper()
        for c in ["open", "high", "low", "close", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        # SSI intraday `Value` is NOT cumulative VND — recompute typical px * vol.
        if {"high", "low", "close", "volume"}.issubset(df.columns):
            tp = (df["high"] + df["low"] + df["close"]) / 3.0
            df["value"] = (tp * df["volume"]).astype(float)
        return self._enforce_ohlcv_schema(df)

    # -- current/last price for a single symbol ------------------------------
    def get_current_price(
        self, symbol: str, trade_date: Optional[date] = None
    ) -> Optional[float]:
        """Last 1-minute bar close (raw VND) for `symbol`, or None if no bar yet.

        NOTE: returns RAW VND (no /1000). `get_current_prices` applies the
        thousand-VND divisor for the breadth contract.
        """
        if trade_date is None:
            trade_date = datetime.now(ICT).date()
        df = self.get_intraday_bars(symbol, trade_date, resolution_minutes=1)
        if df is None or df.empty:
            return None
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if close.empty:
            return None
        px = float(close.iloc[-1])
        return px if px > 0 else None

    # -- current price for many symbols (one REST call each) -----------------
    def get_current_prices(
        self, tickers: list[str], trade_date: Optional[date] = None
    ) -> dict[str, float]:
        """Return {TICKER_UPPER: last_price_in_thousand_VND} for `tickers`.

        Matches the old vnstock `fetch_current_prices` contract exactly:
        keys are UPPER-cased, prices divided by 1000. One REST call per ticker
        (SSI has no batch price board). Symbols with no bar yet (pre-open /
        halted) or any per-symbol error are skipped, never fatal.
        """
        if trade_date is None:
            trade_date = datetime.now(ICT).date()
        out: dict[str, float] = {}
        n = len(tickers)
        for i, ticker in enumerate(tickers, start=1):
            sym = str(ticker).upper().strip()
            if not sym:
                continue
            try:
                px = self.get_current_price(sym, trade_date)
            except Exception as exc:  # one bad symbol must not sink the sweep
                LOGGER.warning("SSI current-price failed for %s (%d/%d): %s",
                               sym, i, n, exc)
                continue
            if px is None or px <= 0:
                LOGGER.debug("No bar yet for %s (%d/%d) — skipping", sym, i, n)
                continue
            out[sym] = px / PRICE_DIVISOR
        LOGGER.info("SSI current prices: %d/%d tickers", len(out), n)
        return out

    # -- daily OHLCV (EOD / RS reuse) ----------------------------------------
    def get_daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Daily OHLCV for `symbol` over [start, end] inclusive, ascending by ts.

        Canonical schema (ts tz-aware ICT). Daily `Value` IS a correct VND
        turnover aggregate, so it's kept as-is (unlike intraday). Provided for
        later EOD/RS reuse; not used by the intraday breadth path.
        """
        rows: list[dict] = []
        page = 1
        while True:
            resp = self._sdk_call_with_retry(
                op="daily_ohlc",
                req=self._model.daily_ohlc(
                    symbol=symbol.upper(),
                    fromDate=start.strftime("%d/%m/%Y"),
                    toDate=end.strftime("%d/%m/%Y"),
                    pageIndex=page, pageSize=1000, ascending=True,
                ),
            )
            chunk = (resp or {}).get("data") or []
            if not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < 1000:
                break
            page += 1

        if not rows:
            return pd.DataFrame(columns=REQUIRED_OHLCV_COLS)
        df = pd.DataFrame(rows).rename(columns={
            "Symbol": "symbol", "TradingDate": "ts", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume", "Value": "value",
        })
        df["ts"] = pd.to_datetime(df["ts"], format="%d/%m/%Y", errors="coerce")
        for c in ["open", "high", "low", "close", "volume", "value"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return self._enforce_ohlcv_schema(df)

    # -- shared helper -------------------------------------------------------
    @staticmethod
    def _enforce_ohlcv_schema(df: pd.DataFrame) -> pd.DataFrame:
        for col in REQUIRED_OHLCV_COLS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[REQUIRED_OHLCV_COLS].copy()
        df = df.dropna(subset=["ts"])
        if df.empty:
            return df.reset_index(drop=True)
        if df["ts"].dt.tz is None:
            df["ts"] = df["ts"].dt.tz_localize(ICT)
        else:
            df["ts"] = df["ts"].dt.tz_convert(ICT)
        return df.sort_values("ts").reset_index(drop=True)


# Module-level convenience: a lazily-built shared client + free functions so
# callers can do `from ssi_client import get_current_prices`.
_SHARED_CLIENT: Optional[SSIClient] = None


def _client() -> SSIClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = SSIClient()
    return _SHARED_CLIENT


def get_current_prices(
    tickers: list[str], trade_date: Optional[date] = None
) -> dict[str, float]:
    """{TICKER_UPPER: last_price_in_thousand_VND}. See SSIClient.get_current_prices."""
    return _client().get_current_prices(tickers, trade_date)


def get_daily_ohlcv(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Daily OHLCV DataFrame. See SSIClient.get_daily_ohlcv."""
    return _client().get_daily_ohlcv(symbol, start, end)


if __name__ == "__main__":
    # Tiny smoke test: print current prices for a few symbols.
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    import sys
    syms = [s.upper() for s in sys.argv[1:]] or ["FPT", "HPG", "VNINDEX"]
    prices = get_current_prices(syms)
    for s in syms:
        print(f"{s}: {prices.get(s)}")
