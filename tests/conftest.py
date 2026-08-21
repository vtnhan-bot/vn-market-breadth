"""Shared fixtures for the data-logic invariant suite.

Hermetic: builds synthetic in-memory data only, no network and no GCS. Modules
under test (market_breadth, intraday_breadth, liquidity_screen, dnse_client)
import with just pandas/numpy/requests; the vnstock-gated ingestion tests use
pytest.importorskip so they run locally and skip cleanly in minimal CI.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 60 business days in early 2026 — safely in the past, so intraday's
# "drop today's row" never touches the fixture.
BDAYS = list(pd.bdate_range("2026-01-01", periods=60))
SESSION_DATES = [d.date() for d in BDAYS]


def ticker_frame(base: float = 10.0, slope: float = 0.1, n: int = 60) -> pd.DataFrame:
    """One uptrending OHLCV frame (close rises so it sits above every SMA)."""
    close = [round(base + i * slope, 4) for i in range(n)]
    return pd.DataFrame({
        "time": SESSION_DATES[:n],
        "open": close,
        "high": [round(c + 0.5, 4) for c in close],
        "low": [round(c - 0.5, 4) for c in close],
        "close": close,
        "volume": [1_000_000] * n,
    })


@pytest.fixture
def price_data():
    """130 uptrending tickers x 60 sessions (a cohort of 100 + 30 extra so the
    top-100 breadth scoping is distinguishable from the full fetch universe)."""
    return {f"T{i:03d}": ticker_frame(base=10 + i * 0.05) for i in range(130)}


@pytest.fixture
def vnindex_df():
    """VNINDEX as combined_dataset stores it: thousand-POINTS (~1.70 == 1,700)."""
    close = [round(1.70 + i * 0.001, 5) for i in range(60)]
    return pd.DataFrame({
        "time": SESSION_DATES,
        "open": close,
        "high": [round(c + 0.005, 5) for c in close],
        "low": [round(c - 0.005, 5) for c in close],
        "close": close,
        "volume": [400_000_000] * 60,
    })


@pytest.fixture
def combined_csv(tmp_path, price_data):
    """Write price_data as a combined_dataset.csv (what intraday reads)."""
    frames = []
    for tkr, df in price_data.items():
        d = df.copy()
        d["ticker"] = tkr
        frames.append(d)
    combined = pd.concat(frames, ignore_index=True)
    path = tmp_path / "combined_dataset.csv"
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    return path
