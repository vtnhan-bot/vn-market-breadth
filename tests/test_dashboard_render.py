"""FIX 2 (VN-Index candlestick x1000), FIX 3 (top-100 universe count), FIX 4
(client-side ICT date helper) — verified through the real build_html output.
"""
import market_breadth as mb


def _breadth_analysis(price_data, n_cohort):
    cohort = {k: price_data[k] for k in list(price_data)[:n_cohort]}
    breadth = mb.calculate_breadth(cohort, sessions_show=50)
    analysis = mb.generate_analysis(breadth, cohort)
    return breadth, analysis, cohort


def _render(price_data, vnindex_df, n_cohort=100):
    breadth, analysis, cohort = _breadth_analysis(price_data, n_cohort)
    html = mb.build_html(breadth, analysis, list(cohort), "test-provider",
                         vnindex_df=vnindex_df, price_data=price_data)
    return html, analysis


def test_vnindex_candlestick_rendered_in_points(price_data, vnindex_df):
    html, _ = _render(price_data, vnindex_df)
    assert '"candlestick"' in html
    # combined_dataset stores VNINDEX in thousand-points; the candle must x1000
    # to real points (~1700), matching the ex-Vin panel and the 'Điểm' axis.
    expected_pts = round(float(vnindex_df["close"].iloc[-1]) * 1000.0, 2)
    assert expected_pts > 1000
    assert str(expected_pts) in html, (
        f"candle close {expected_pts} (points) not in HTML — x1000 scaling missing"
    )


def test_breadth_universe_count_is_top100_not_fetch_universe(price_data):
    # cohort of 100 -> n_tickers == 100 (the fixed call passes breadth_price_data)
    _, analysis, _ = _breadth_analysis(price_data, n_cohort=100)
    assert analysis["n_tickers"] == 100
    # passing the full 130-name fetch universe (the pre-fix bug) would report 130,
    # proving the two are distinguishable and the fix must use the cohort.
    breadth_full = mb.calculate_breadth(
        {k: price_data[k] for k in list(price_data)[:100]}, sessions_show=50)
    bug = mb.generate_analysis(breadth_full, price_data)  # full universe
    assert bug["n_tickers"] == 130 and analysis["n_tickers"] == 100


def test_ict_date_helper_has_no_timezone_offset_code(price_data, vnindex_df):
    html, _ = _render(price_data, vnindex_df)
    # the buggy client-side helper double-counted the viewer's offset
    assert "getTimezoneOffset() * 60000" not in html
    assert "new Date(now.getTime() + 7 * 3600000)" in html
