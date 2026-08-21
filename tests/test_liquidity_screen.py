"""FIX 5 (liquidity fail-safe status) + the screen's core invariants: the two-band
hysteresis, cold-start (new listing vs old rare-trader), and the fail-safe reads.
"""
import pandas as pd

import liquidity_screen as ls

SESSIONS = [d.date() for d in pd.bdate_range("2026-01-01", periods=60)]
# adv = median(close*volume)*1000, close fixed at 10 -> volume = adv/10000.
CLOSE = 10.0


def _rows(ticker, traded_session_idxs, adv_vnd):
    vol = adv_vnd / (CLOSE * 1000.0)
    return [{"ticker": ticker, "time": SESSIONS[i], "close": CLOSE, "volume": vol}
            for i in traded_session_idxs]


def _members(tmp_path, statuses):
    df = pd.DataFrame({
        "ticker": list(statuses), "coverage": [1.0] * len(statuses),
        "median_adv_vnd": [2e9] * len(statuses), "nbars": [60] * len(statuses),
        "status": list(statuses.values()),
    })
    path = tmp_path / "rs_screen_members.csv"
    ls.write_members(path, df, screened_at="2026-08-21")
    return path


# ---- FIX 5: fail-safe status='failsafe' reads back as None ----
def test_failsafe_status_reads_back_none(tmp_path):
    # rs_matrix_3T writes every name status='failsafe' on the FAILSAFE_MIN_KEPT
    # path; read_kept_members must return None so consumers fall back to full THIS
    # run AND next-run prior_members is None (strict ENTRY band, no leak).
    path = _members(tmp_path, {"AAA": "failsafe", "BBB": "failsafe", "CCC": "failsafe"})
    assert ls.read_kept_members(path) is None


def test_kept_and_coldstart_are_members_dropped_is_not(tmp_path):
    path = _members(tmp_path, {"AAA": "kept", "BBB": "coldstart", "CCC": "dropped"})
    assert ls.read_kept_members(path) == {"AAA", "BBB"}


def test_missing_or_empty_file_fails_safe_to_none(tmp_path):
    assert ls.read_kept_members(tmp_path / "does_not_exist.csv") is None
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert ls.read_kept_members(empty) is None


# ---- Two-band hysteresis + cold-start ----
def test_entry_band_is_strict_keep_band_is_loose():
    all_idx = list(range(60))
    marginal = list(range(58))          # coverage 58/60 = 0.967 (>=0.95, <0.98)
    combined = pd.DataFrame(
        _rows("LIQUID", all_idx, 2e9)             # cov 1.0, adv 2bn
        + _rows("MARGINAL", marginal, 0.85e9)     # cov 0.967, adv 0.85bn
        + _rows("FALLEN", list(range(54)), 0.85e9)  # cov 0.90, adv 0.85bn
    )
    tickers = ["LIQUID", "MARGINAL", "FALLEN"]

    # No prior -> ENTRY band (>=0.98 AND >=1bn)
    kept, dropped, _ = ls.screen_universe(combined, tickers, SESSIONS, prior_members=set())
    assert "LIQUID" in kept
    assert "MARGINAL" in dropped, "0.967 cov / 0.85bn fails the strict ENTRY band"

    # MARGINAL previously kept -> looser KEEP band (drop only if <0.95 OR <0.8bn)
    kept2, dropped2, _ = ls.screen_universe(
        combined, tickers, SESSIONS, prior_members={"MARGINAL", "FALLEN"})
    assert "MARGINAL" in kept2, "an incumbent at 0.967/0.85bn survives the KEEP band"
    assert "FALLEN" in dropped2, "0.90 cov breaches the KEEP coverage floor"


def test_coldstart_exempts_new_listing_but_not_old_rare_trader():
    # NEW: first bar only in the last 10 window sessions -> n_possible < 20 -> coldstart
    new_listing = list(range(50, 60))
    # OLD_RARE: listed at session 0 but trades sparsely -> n_possible=60, low coverage
    old_rare = list(range(0, 60, 2))  # 30 sessions
    combined = pd.DataFrame(
        _rows("NEWLISTING", new_listing, 0.2e9)   # tiny adv, but genuinely new
        + _rows("OLDRARE", old_rare, 0.2e9)       # old + illiquid
    )
    kept, dropped, stats = ls.screen_universe(
        combined, ["NEWLISTING", "OLDRARE"], SESSIONS, prior_members=set())
    assert "NEWLISTING" in kept, "a genuinely new listing is cold-start exempt"
    assert "OLDRARE" in dropped, "an old rarely-trading name is screened out, not exempt"
    status = dict(zip(stats["ticker"], stats["status"]))
    assert status["NEWLISTING"] == "coldstart"
