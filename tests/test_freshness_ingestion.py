"""Freshness guard (the 2.5-week T-1 stale-publish bug) + ingestion sanity
(degenerate-OHLC drop). These live in eod_batch_downloader, which hard-imports
vnstock, so they run locally and skip in the minimal (no-vnstock) CI job.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ICT = ZoneInfo("Asia/Ho_Chi_Minh")


def test_postclose_cache_requires_todays_bar():
    """The stale-dashboard bug: a post-close run must NOT reuse a T-1 snapshot as T."""
    pytest.importorskip("vnstock")
    import eod_batch_downloader as eod

    t1 = pd.DataFrame({"time": [date(2026, 8, 19)]})   # snapshot stops at T-1
    t0 = pd.DataFrame({"time": [date(2026, 8, 20)]})   # snapshot carries today's bar
    post_close = datetime(2026, 8, 20, 16, 0, tzinfo=ICT)   # >= POST_CLOSE_SETTLE_HOUR
    pre_close = datetime(2026, 8, 20, 10, 0, tzinfo=ICT)

    # post-close + only T-1 -> must refetch (the bug reused it and republished T-1)
    assert eod.cache_entry_is_current(t1, "2026-08-20", post_close) is False
    # post-close + today's bar present -> reuse is fine
    assert eod.cache_entry_is_current(t0, "2026-08-20", post_close) is True
    # pre-close -> any same-day entry is reusable
    assert eod.cache_entry_is_current(t1, "2026-08-20", pre_close) is True


def test_drop_degenerate_ohlc_rows():
    """Impossible bars must be dropped before caching (else they go false-bearish)."""
    pytest.importorskip("vnstock")
    import eod_batch_downloader as eod

    df = pd.DataFrame({
        "time": [date(2026, 8, d) for d in range(3, 8)],
        "open":   [10.0, 10.0, 10.0, 10.0, 10.0],
        "high":   [11.0, 11.0,  9.0, 11.0, 11.0],   # row2: high < low
        "low":    [ 9.0,  9.0, 10.0,  9.0,  9.0],
        "close":  [10.0,  0.0, 10.5, 12.0, 10.0],   # row1: close<=0 ; row3: close>high
        "volume": [100.0, 100.0, 100.0, 100.0, -5.0],  # row4: negative volume
    })
    out = eod._drop_degenerate_ohlc_rows(df, "TEST")
    assert len(out) == 1, "only the first (valid) row survives"
    assert out["close"].iloc[0] == 10.0
