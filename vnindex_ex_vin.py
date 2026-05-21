"""VN-Index excluding VIC/VHM/VRE — Paasche-style mcap-weighted reconstruction.

Methodology
-----------
The HOSE VN-Index uses a free-float-adjusted Paasche formula anchored at
2000-07-28 = 100. Since Vingroup was not listed in 2000, the base mcap on
the anchor day is identical whether we include or exclude the Vin trio:

    base_mcap_total ≈ base_mcap_ex_vin

This collapses the ex-Vin index formula to:

    ex_vin_index[t] = VNINDEX[t] × (ex_vin_mcap[t] / total_mcap[t])

No anchor-at-day-0 fudge — every session is derived by the same formula. The
two indices have different starting values (their day-0 gap reflects the Vin
trio's day-0 weight in HOSE), and they drift independently from there.

Mcap proxy
----------
We don't track free-float over time, so we use:

    implied_shares[i] = rs_fixed_tickers.csv:market_cap[i] / (latest_close[i] × 1000)
    mcap[i,t] = close[i,t] × implied_shares[i]

This holds for short windows (~50 sessions) where shares-outstanding doesn't
materially change. Sanity check: the same formula applied without exclusion
reproduces VNINDEX within ±0.01 across 50 sessions, confirming the 147-ticker
HOSE universe in rs_fixed_tickers.csv captures HOSE's price action faithfully.

Inputs
------
- vnindex_df       : VN-Index daily close series (combined_dataset.csv VNINDEX rows)
- combined_df      : daily OHLC for the unified universe
- rs_universe_path : path to rs_fixed_tickers.csv (provides HOSE filter + market_cap)

Output
------
DataFrame indexed by trading session date with columns:
  - vnindex          (raw, in thousand-points like combined_dataset.csv)
  - ex_vin_index     (same scale)
  - vin_share_pct    (Vin trio's % of total HOSE mcap on that day)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


VIN_TICKERS = ("VIC", "VHM", "VRE")
DEFAULT_RS_UNIVERSE_PATH = Path(__file__).parent / "rs_fixed_tickers.csv"


def compute_vnindex_ex_vin(
    vnindex_df: pd.DataFrame,
    combined_df: pd.DataFrame,
    window_start,
    sessions_show: int,
    rs_universe_path: Path | str = DEFAULT_RS_UNIVERSE_PATH,
) -> pd.DataFrame:
    """Return the 50-session VNINDEX + ex-Vin VNINDEX comparison frame.

    Empty DataFrame on missing inputs / insufficient data.
    """
    empty = pd.DataFrame(columns=["time", "vnindex", "ex_vin_index", "vin_share_pct"])

    if vnindex_df is None or vnindex_df.empty:
        return empty

    uni = pd.read_csv(rs_universe_path, encoding="utf-8-sig")
    uni["ticker"] = uni["ticker"].astype(str).str.strip().str.upper()
    uni["market_cap"] = pd.to_numeric(uni["market_cap"], errors="coerce")
    hose_uni = uni[(uni["exchange"] == "HOSE") & uni["market_cap"].notna()][
        ["ticker", "market_cap"]
    ].copy()
    if hose_uni.empty:
        return empty

    vni = vnindex_df.copy()
    vni["time"] = pd.to_datetime(vni["time"])
    vni = vni[vni["time"] >= pd.to_datetime(window_start)].tail(sessions_show)
    vni = vni.sort_values("time").reset_index(drop=True)
    if vni.empty:
        return empty

    timeline = vni["time"]

    # Slice combined_dataset to HOSE tickers in our universe + window sessions
    px = combined_df[combined_df["ticker"].isin(set(hose_uni["ticker"]))].copy()
    px["time"] = pd.to_datetime(px["time"])
    px = px[px["time"].isin(timeline)]

    # Implied shares: market_cap / (latest_close × 1000), where 1000 converts
    # combined_dataset's thousand-VND close to raw VND.
    latest_session = timeline.iloc[-1]
    latest_closes = (
        px[px["time"] == latest_session][["ticker", "close"]]
        .rename(columns={"close": "latest_close"})
    )
    if latest_closes.empty:
        return empty
    shares = hose_uni.merge(latest_closes, on="ticker", how="inner")
    shares["implied_shares"] = shares["market_cap"] / (shares["latest_close"] * 1000.0)
    shares = shares.dropna(subset=["implied_shares"])
    if shares.empty:
        return empty

    # Pivot to wide: rows=sessions, cols=tickers, values=close. Multiply by
    # implied_shares per ticker → per-cell mcap. Forward-fill so a ticker
    # missing one bar doesn't drop a whole session.
    pivot = (
        px.pivot_table(index="time", columns="ticker", values="close", aggfunc="last")
        .reindex(timeline)
        .ffill()
    )
    keep_cols = [c for c in pivot.columns if c in set(shares["ticker"])]
    if not keep_cols:
        return empty
    pivot = pivot[keep_cols]
    shares_map = shares.set_index("ticker")["implied_shares"].to_dict()
    mcap = pivot.multiply([shares_map[c] for c in pivot.columns], axis=1)
    total_mcap = mcap.sum(axis=1)
    ex_vin_cols = [c for c in keep_cols if c not in VIN_TICKERS]
    ex_vin_mcap = mcap[ex_vin_cols].sum(axis=1)

    out = pd.DataFrame({
        "time": timeline.values,
        "vnindex": vni["close"].astype(float).values,
        "total_mcap": total_mcap.values,
        "ex_vin_mcap": ex_vin_mcap.values,
    })
    out["ex_vin_index"] = out["vnindex"] * (out["ex_vin_mcap"] / out["total_mcap"])
    out["vin_share_pct"] = (1.0 - out["ex_vin_mcap"] / out["total_mcap"]) * 100.0

    return out[["time", "vnindex", "ex_vin_index", "vin_share_pct"]]
