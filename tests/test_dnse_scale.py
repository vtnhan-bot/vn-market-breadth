"""DNSE scale contract: the transport returns NATIVE scale (stocks thousand-VND,
index raw points), and the pipeline applies x1 to stocks / /1000 to the index so
everything lands in combined_dataset's thousand-VND (stocks) / thousand-points
(VNINDEX) convention. A scale slip here is a 1000x error on the dashboard.
"""
from datetime import date

import pandas as pd
import pytest

import dnse_client


def test_is_index_classification():
    assert dnse_client.is_index("VNINDEX")
    assert dnse_client.is_index("vnindex")
    assert not dnse_client.is_index("FPT")
    assert not dnse_client.is_index("DHC")


def test_get_daily_ohlcv_returns_native_scale(monkeypatch):
    # chart-api native payload: stocks in thousand-VND, index in raw points.
    payload = {
        "t": [int(pd.Timestamp("2026-08-19").timestamp()),
              int(pd.Timestamp("2026-08-20").timestamp())],
        "o": [36.0, 36.2], "h": [36.6, 36.8], "l": [35.8, 36.0],
        "c": [36.55, 36.70], "v": [500000, 600000], "nextTime": 0,
    }
    monkeypatch.setattr(dnse_client, "_get_json", lambda seg, params: payload)
    df = dnse_client.get_daily_ohlcv("DHC", date(2026, 8, 19), date(2026, 8, 20))
    assert list(df["close"]) == [36.55, 36.70], "transport must not rescale (native)"
    assert list(df["volume"]) == [500000, 600000], "volume stays raw shares"


def test_pipeline_scales_index_by_1000_and_stock_by_1(monkeypatch):
    pytest.importorskip("vnstock")  # eod_batch_downloader hard-imports vnstock
    import eod_batch_downloader as eod

    idx_native = pd.DataFrame({
        "time": [date(2026, 8, 20)], "open": [1730.0], "high": [1735.0],
        "low": [1725.0], "close": [1734.24], "volume": [4e8],
    })
    monkeypatch.setattr(dnse_client, "get_daily_ohlcv", lambda *a, **k: idx_native)
    out_idx = eod._fetch_dnse_daily("VNINDEX", "2026-08-19", "2026-08-20")
    assert abs(out_idx["close"].iloc[0] - 1.73424) < 1e-9, "VNINDEX must be /1000 -> thousand-points"

    stk_native = pd.DataFrame({
        "time": [date(2026, 8, 20)], "open": [36.2], "high": [36.8],
        "low": [35.8], "close": [36.55], "volume": [5e5],
    })
    monkeypatch.setattr(dnse_client, "get_daily_ohlcv", lambda *a, **k: stk_native)
    out_stk = eod._fetch_dnse_daily("DHC", "2026-08-19", "2026-08-20")
    assert abs(out_stk["close"].iloc[0] - 36.55) < 1e-9, "a stock stays x1 (already thousand-VND)"
