"""Corporate-action ex-date handling in the intraday RS column.

Regression for AGG 2026-09-04: a HOSE stock's reference was reset by an ex-date
(prior close 11.90 -> ~10.80), so it opened -8.8% (impossible on HOSE's +-7% band).
The intraday RS compared the post-ex live price against the UNADJUSTED prior close
and published daily_change_pct = -10.08% (real move -1.39%) with a depressed rating.
The fix detects the reset (open gaps beyond the exchange limit) and ex-adjusts that
ticker's history by the open-implied factor, so the whole cell is post-ex consistent.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import intraday_rs_3T as irs

ICT = ZoneInfo("Asia/Ho_Chi_Minh")


def _write_history(tmp_path):
    # 60 business days of gently-rising closes for two HOSE names.
    dates = list(pd.bdate_range("2026-06-01", periods=60))
    rows = []
    for tkr, base in (("AGG", 11.0), ("CTRL", 20.0)):
        for i, d in enumerate(dates):
            c = round(base + i * 0.01, 4)
            rows.append({"ticker": tkr, "time": d.date().isoformat(),
                         "open": c, "high": c + 0.1, "low": c - 0.1,
                         "close": c, "volume": 100000, "source": "DNSE"})
    path = tmp_path / "combined_dataset.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    prior_close = round(11.0 + 59 * 0.01, 4)   # AGG's last close = 11.59
    ctrl_prior = round(20.0 + 59 * 0.01, 4)    # CTRL's last close = 20.59
    today = dates[-1].date() + timedelta(days=3)
    return path, prior_close, ctrl_prior, today


def test_exdate_reset_is_ex_adjusted_via_open(tmp_path, monkeypatch):
    path, agg_prior, ctrl_prior, today = _write_history(tmp_path)
    # AGG ex-date: opens ~8.8% below the prior close (reference reset), now a bit lower.
    agg_open, agg_last = round(agg_prior * 0.9118, 4), round(agg_prior * 0.9118 * 0.985, 4)
    # CTRL: a normal day, opens near its prior close.
    ctrl_open, ctrl_last = ctrl_prior, round(ctrl_prior * 1.01, 4)

    monkeypatch.setattr(irs, "_load_rs_universe", lambda: ["AGG", "CTRL"])
    monkeypatch.setattr(irs, "_load_exchange_map", lambda: {"AGG": "HOSE", "CTRL": "HOSE"})
    monkeypatch.setattr(irs, "_fetch_intraday_prices",
                        lambda tks: {"AGG": (agg_open, agg_last), "CTRL": (ctrl_open, ctrl_last)})

    now_ict = datetime(today.year, today.month, today.day, 11, 16, tzinfo=ICT)
    payload = irs.compute_intraday_rs(path, now_ict)
    assert payload is not None
    by = {r["ticker"]: r for r in payload["rows"]}

    # AGG: daily % is vs the ex-adjusted reference (~the open), NOT the raw prior close.
    exadj = round((agg_last / agg_open - 1.0) * 100, 2)     # ~ -1.5%
    raw = (agg_last / agg_prior - 1.0) * 100                # ~ -10%  (the bug)
    assert by["AGG"]["daily_change_pct"] == exadj, (
        f"AGG must be ex-adjusted ({exadj}%), got {by['AGG']['daily_change_pct']}%"
    )
    assert raw < -8 and by["AGG"]["daily_change_pct"] > -5, "must not report the ~-10% phantom gap"

    # CTRL: no reset -> daily % is vs the raw prior close, unchanged behaviour.
    assert by["CTRL"]["daily_change_pct"] == round((ctrl_last / ctrl_prior - 1.0) * 100, 2)


def test_normal_move_at_limit_is_not_treated_as_reset(tmp_path, monkeypatch):
    # A legit HOSE stock at the -7% floor must NOT trip the reset detector: it OPENS
    # within the band (near prior close) and only later trades down to the floor.
    path, agg_prior, ctrl_prior, today = _write_history(tmp_path)
    open_near = agg_prior * 0.999           # opens ~flat
    last_floor = round(agg_prior * 0.93, 4)  # trades to the -7% floor intraday

    monkeypatch.setattr(irs, "_load_rs_universe", lambda: ["AGG"])
    monkeypatch.setattr(irs, "_load_exchange_map", lambda: {"AGG": "HOSE"})
    monkeypatch.setattr(irs, "_fetch_intraday_prices",
                        lambda tks: {"AGG": (open_near, last_floor)})

    now_ict = datetime(today.year, today.month, today.day, 11, 16, tzinfo=ICT)
    payload = irs.compute_intraday_rs(path, now_ict)
    by = {r["ticker"]: r for r in payload["rows"]}
    # vs the raw prior close (no ex-adjust): ~ -7%.
    assert by["AGG"]["daily_change_pct"] == round((last_floor / agg_prior - 1.0) * 100, 2)
