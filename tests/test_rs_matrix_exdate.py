"""EOD RS matrix: corporate-action ex-date on the latest session is ex-adjusted
(parity with the intraday RS fix). Regression for AGG 2026-09-04.
"""
import pandas as pd
import pytest

rs = pytest.importorskip("rs_matrix_3T")  # imports rs_source2 -> vnstock; skipped in minimal CI


def _combined(tmp_path):
    dates = list(pd.bdate_range("2026-05-01", periods=70))
    last = dates[-1].date()
    rows = []

    def series(tkr, base, slope, ex=False):
        for i, d in enumerate(dates):
            c = round(base + i * slope, 4)
            o = c
            if ex and i == len(dates) - 1:
                # ex-date: reference reset ~-8.8%, opens at the adjusted ref, tiny move.
                prev = round(base + (i - 1) * slope, 4)
                o = round(prev * 0.9118, 4)
                c = round(o * 0.99, 4)
            rows.append({"ticker": tkr, "time": d.date().isoformat(),
                         "open": o, "high": max(o, c) + 0.1, "low": min(o, c) - 0.1,
                         "close": c, "volume": 500000, "source": "DNSE"})

    series("VNINDEX", 1.70, 0.001)         # benchmark
    series("AGG", 10.0, 0.02, ex=True)     # ex-date on the last session
    series("CTRL1", 20.0, 0.03)
    series("CTRL2", 30.0, -0.01)
    series("CTRL3", 15.0, 0.015)
    path = tmp_path / "combined_dataset.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    agg_prev = round(10.0 + 68 * 0.02, 4)   # AGG close on the session before the ex-date
    return path, last, agg_prev


def _universe():
    names = ["AGG", "CTRL1", "CTRL2", "CTRL3"]
    return pd.DataFrame({
        "ticker": names, "company_name": names, "exchange": ["HOSE"] * 4,
        "market_cap": [1e9] * 4, "industry": ["x"] * 4, "universe_order": range(1, 5),
    })


def test_exdate_latest_session_is_ex_adjusted(tmp_path, monkeypatch):
    combined, last, agg_prev = _combined(tmp_path)
    monkeypatch.setattr(rs, "SCRIPT_DIR", tmp_path)  # only for the relative_to() log line
    monkeypatch.setattr(rs, "RS_MATRIX_3T_PATH", tmp_path / "rs_matrix_3T.csv")
    monkeypatch.setattr(rs, "RS_SCREEN_MEMBERS_PATH", tmp_path / "rs_screen_members.csv")

    rs.build_rs_matrix(_universe(), combined)
    out = pd.read_csv(tmp_path / "rs_matrix_3T.csv")
    out["session_date"] = pd.to_datetime(out["session_date"]).dt.date

    agg_last = out[(out["ticker"] == "AGG") & (out["session_date"] == last)]
    assert not agg_last.empty, "AGG's ex-date row must be produced"
    chg = float(agg_last.iloc[0]["daily_change_pct"])
    ex_open = round(agg_prev * 0.9118, 4)
    ex_close = round(ex_open * 0.99, 4)
    expected_exadj = round((ex_close / ex_open - 1.0) * 100, 2)   # ~ -1.0%
    raw_phantom = (ex_close / agg_prev - 1.0) * 100               # ~ -9.7%  (the bug)
    assert abs(chg - expected_exadj) < 0.15, f"AGG ex-date % must be ex-adjusted (~{expected_exadj}), got {chg}"
    assert raw_phantom < -8 and chg > -4, "must not store the ~-10% phantom gap"

    # A control's latest-session % is the raw close-to-close move (no ex-adjust).
    c1 = out[(out["ticker"] == "CTRL1") & (out["session_date"] == last)]
    assert not c1.empty
    assert -7 < float(c1.iloc[0]["daily_change_pct"]) < 7, "control must not be treated as a reset"
