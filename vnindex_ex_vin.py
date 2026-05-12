"""
VNINDEX ex-Vingroup (VIC, VHM, VRE) reconstruction.

Method
------
We hold the published Vin-trio aggregate weight constant on day 0 of the
window and assume HOSE's total float market cap scales proportionally with
VNINDEX over the (short) 50-session window. This is unit-scaling-independent —
the math depends only on prices, share counts and the Vin-trio weight, not on
absolute VND-per-index-point.

For each session t in the window (anchored at day 0):
    FFMC_Vin[t]    = sum_{i in VIC,VHM,VRE} price_i[t] * shares_i * free_float_i
    MCAP_HOSE[0]   = FFMC_Vin[0] / VIN_TRIO_WEIGHT       (calibrate at start)
    MCAP_HOSE[t]   = MCAP_HOSE[0] * VNINDEX[t] / VNINDEX[0]
    MCAP_ex[t]     = MCAP_HOSE[t] - FFMC_Vin[t]
    ex_index[t]    = VNINDEX[0] * MCAP_ex[t] / MCAP_ex[0]

By construction ex_index[0] == VNINDEX[0], so both series start at the same
point and diverge over the window.

Constants must be refreshed quarterly:
- FREE_FLOAT: when HOSE publishes a free-float band review.
- SHARES_OUTSTANDING: when a Vin issuer executes a stock dividend / split / new issuance.
- VIN_TRIO_WEIGHT: when HOSE publishes new constituent weights.
"""

from __future__ import annotations

import pandas as pd

VIN_TICKERS = ("VIC", "VHM", "VRE")

# HOSE-published free-float bands (rounded to nearest 5%).
# Last verified: 2026-05-12 — VERIFY against HOSE's latest constituent disclosure
# before relying on this for decisions.
FREE_FLOAT = {
    "VIC": 0.35,
    "VHM": 0.30,
    "VRE": 0.40,
}

# Outstanding shares (raw count). Refresh on corporate actions.
# Last verified: 2026-05-12 — VERIFY against issuer disclosures.
SHARES_OUTSTANDING = {
    "VIC": 3_823_661_561,
    "VHM": 4_354_059_900,
    "VRE": 2_272_318_410,
}

# HOSE-published combined free-float weight of VIC + VHM + VRE inside VNINDEX
# on the most recent constituent-weights report. Used to calibrate the implied
# HOSE total float market cap on day 0 of the chart window — eliminates the
# need for an absolute VND-per-index-point constant and makes the calculation
# robust to dataset price-scaling conventions. Refresh quarterly.
VIN_TRIO_WEIGHT = 0.055  # 5.5%


def compute_vnindex_ex_vin(
    vnindex_df: pd.DataFrame,
    combined_df: pd.DataFrame,
    window_start,
    sessions_show: int,
) -> pd.DataFrame:
    """
    Build the VNINDEX ex-Vin trio series aligned to VNINDEX over the last
    `sessions_show` sessions starting at or after `window_start`.

    Parameters
    ----------
    vnindex_df : DataFrame with 'time' and 'close' columns (VNINDEX rows only).
    combined_df : the full combined_dataset.csv DataFrame; must contain VIC/VHM/VRE rows.
    window_start : pandas.Timestamp or compatible — start of the window (inclusive).
    sessions_show : int — max sessions on the chart (matches the VNINDEX chart).

    Returns
    -------
    DataFrame with columns ['time', 'vnindex_close', 'ex_vin_close'] indexed
    chronologically over the window, or empty DataFrame if data is insufficient.
    """
    if vnindex_df is None or vnindex_df.empty:
        return pd.DataFrame(columns=["time", "vnindex_close", "ex_vin_close"])

    vni = vnindex_df.copy()
    vni["time"] = pd.to_datetime(vni["time"])
    vni = vni[vni["time"] >= pd.to_datetime(window_start)].tail(sessions_show)
    vni = vni.sort_values("time").reset_index(drop=True)
    if vni.empty:
        return pd.DataFrame(columns=["time", "vnindex_close", "ex_vin_close"])

    vin_df = combined_df[combined_df["ticker"].isin(VIN_TICKERS)].copy()
    vin_df["time"] = pd.to_datetime(vin_df["time"])
    vin_close = (
        vin_df.pivot_table(index="time", columns="ticker", values="close", aggfunc="last")
        .reindex(vni["time"])
        .ffill()
    )

    missing = [t for t in VIN_TICKERS if t not in vin_close.columns]
    if missing or vin_close.isna().any().any():
        return pd.DataFrame(columns=["time", "vnindex_close", "ex_vin_close"])

    ffmc_vin = sum(
        vin_close[t].values * SHARES_OUTSTANDING[t] * FREE_FLOAT[t]
        for t in VIN_TICKERS
    )

    vnindex_close = vni["close"].astype(float).values
    if vnindex_close[0] <= 0 or ffmc_vin[0] <= 0:
        return pd.DataFrame(columns=["time", "vnindex_close", "ex_vin_close"])

    mcap_hose_0 = ffmc_vin[0] / VIN_TRIO_WEIGHT
    mcap_hose = mcap_hose_0 * (vnindex_close / vnindex_close[0])
    mcap_ex = mcap_hose - ffmc_vin

    if mcap_ex[0] <= 0:
        return pd.DataFrame(columns=["time", "vnindex_close", "ex_vin_close"])

    ex_vin_close = vnindex_close[0] * mcap_ex / mcap_ex[0]

    return pd.DataFrame({
        "time": vni["time"].values,
        "vnindex_close": vnindex_close.round(4),
        "ex_vin_close": ex_vin_close.round(4),
    })
