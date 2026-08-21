"""FIX 1 (intraday breadth denominator) + the intraday universe contract.

Invariant: an intraday breadth % must be computed over the PRICED set only — a
top-100 name with a valid T-1 SMA but no live price contributes to NEITHER the
numerator nor the denominator. The pre-fix bug counted such names in n_total
(the denominator) only, systematically deflating the live line by priced/total.
"""
import intraday_breadth as ib


def test_denominator_counts_only_priced_tickers(combined_csv, price_data):
    top100 = list(price_data)[:100]
    full = {t: 9999.0 for t in top100}          # every name has a live price
    partial = {t: 9999.0 for t in top100[:60]}  # only 60 names printed a bar

    r_full = ib.compute_breadth(combined_csv, top100, full)
    r_partial = ib.compute_breadth(combined_csv, top100, partial)

    # Denominator (sample_size) tracks the priced set, not the SMA-valid set.
    assert r_full["sample_size"] == 100
    assert r_partial["sample_size"] == 60, (
        "denominator must equal the priced count (60), not all 100 SMA-valid names"
    )


def test_percentage_uses_priced_denominator(combined_csv, price_data):
    top100 = list(price_data)[:100]
    # 60 priced, all above their (uptrending) SMA -> 60/60 == 100%%.
    # The pre-fix bug would report 60/100 == 60%% for the same market.
    partial = {t: 9999.0 for t in top100[:60]}
    res = ib.compute_breadth(combined_csv, top100, partial)
    assert res["mbz3"] == 100.0, (
        f"60 priced all above SMA must read 100%% (priced denom), got {res['mbz3']}"
    )


def test_no_priced_tickers_yields_none(combined_csv, price_data):
    top100 = list(price_data)[:100]
    res = ib.compute_breadth(combined_csv, top100, {})  # nothing priced
    assert res["sample_size"] == 0
    assert res["mbz3"] is None
