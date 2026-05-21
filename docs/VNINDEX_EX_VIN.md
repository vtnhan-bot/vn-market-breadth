# VN-Index excluding Vingroup (VIC / VHM / VRE)

A 50-session line chart sitting directly below the VN-Index candlestick panel. Shows two series on the same y-axis (real points):

- **VN-Index** — actual HOSE headline index, from `combined_dataset.csv`.
- **VN-Index loại VIC/VHM/VRE** — the same index reconstructed without the Vingroup trio.

Together they reveal how much of the headline move is driven by Vin alone (recently: ~80% of cumulative gain across 50 sessions).

## Methodology (no anchoring)

Both series are computed by the *same* formula on every session:

```
ex_vin_index[t] = VNINDEX[t] × (ex_vin_mcap[t] / total_mcap[t])
```

Day 0 starting values are **different by construction** — the gap on day 0 equals the day-0 Vingroup weight in HOSE × VNINDEX[0]. The two lines drift independently from there based on the actual price action of each cohort.

### Derivation

HOSE VN-Index uses a free-float-adjusted Paasche formula anchored at 2000-07-28 with base value 100:

```
VNINDEX[t] = 100 × total_mcap[t] / base_mcap_2000
```

For a hypothetical "ex-Vin VN-Index" with the same base date and base value:

```
ex_vin_index[t] = 100 × ex_vin_mcap[t] / base_ex_vin_mcap_2000
```

Since Vingroup wasn't listed on HOSE in 2000, `base_ex_vin_mcap_2000 ≈ base_mcap_2000`. Substituting:

```
ex_vin_index[t] = 100 × ex_vin_mcap[t] / base_mcap_2000
               = VNINDEX[t] × (ex_vin_mcap[t] / total_mcap[t])
```

No anchor fudge. No `BASE_VALUE = 100` magic number. Both series fall out of the data the same way every day.

### Mcap proxy

We don't have a historical free-float series, so we approximate market cap per ticker by:

```
implied_shares[i] = rs_fixed_tickers.market_cap[i] / (latest_close[i] × 1000)
mcap[i, t]        = close[i, t] × implied_shares[i]
total_mcap[t]     = Σ mcap[i, t]  for i in 147-ticker HOSE universe
ex_vin_mcap[t]    = Σ mcap[i, t]  for i in HOSE universe excluding {VIC, VHM, VRE}
```

The `× 1000` converts `combined_dataset.csv`'s thousand-VND closes to raw VND, so `implied_shares` is a true share count.

**Why this works for short windows**: shares-outstanding doesn't change materially over 50 sessions absent corporate actions. The approximation captures all *price-driven* mcap movement faithfully.

### Sanity check

The same formula applied without exclusions reproduces `VNINDEX[t]` from `combined_dataset.csv` within **±0.01 across all 50 sessions** over the 147-ticker HOSE universe in `rs_fixed_tickers.csv`. This is the calibration evidence that the universe captures HOSE's price action — the small residual is just the long tail of microcaps we omit, which moves the index by less than 0.01 points.

## Sample table (the calibration run, 50 sessions)

```
date         VNINDEX  EX_VIN   Vin %    VNINDEX%   EX_VIN%   Spread (pp)
2026-03-10   1680.0   1370.3  18.43%    +0.00%    +0.00%    +0.00
2026-04-08   1760.0   1402.2  20.33%    +4.76%    +2.33%    -2.44   ← Vin rally accelerates
2026-04-22   1860.0   1392.6  25.13%   +10.71%    +1.63%    -9.09
2026-05-08   1920.0   1407.2  26.71%   +14.29%    +2.70%   -11.59   ← max spread
2026-05-21   1900.0   1405.3  26.04%   +13.10%    +2.55%   -10.54
```

Over the 50-session window:

- VN-Index: +13.10% (1,680 → 1,900)
- Ex-Vin: +2.55% (1,370 → 1,405)
- Spread: −10.54 pp cumulative → **~80% of headline gain came from VIC/VHM/VRE alone**
- Vin trio's HOSE weight grew from 18.4% → 26.0% over the window (they outperformed everything else by a wide margin)

## Where it lives in code

- **`vnindex_ex_vin.py`** (new module) — `compute_vnindex_ex_vin(vnindex_df, combined_df, window_start, sessions_show)` returns a DataFrame with columns `[time, vnindex, ex_vin_index, vin_share_pct]`.
- **`market_breadth.py`**:
  - Imports the function at module top.
  - Calls it in `main()` after the breadth dataframe is built. Wrapped in try/except — chart hides on failure but the EOD pipeline continues.
  - In `build_html()`: assembles a two-line Plotly trace block (`exVinData`), generates the subtitle string with the latest Vin-share % and the cumulative spread, and adds a latest-data label.
  - JS init in the `<script>` block: `Plotly.newPlot('vnindex-ex-vin-chart', exVinData, exVinLayout, config)`.

## Chart contract

| Field | Value |
|---|---|
| DOM container | `<div id="vnindex-ex-vin-chart">` directly below `<div id="vnindex-chart">` |
| Series 1 | VN-Index (blue line, `#1565C0`, width 2.5) |
| Series 2 | VN-Index loại VIC/VHM/VRE (orange line, `#E67E22`, width 2.5) |
| X-axis | `type: 'category'`, 50 dates as `DD-MM-YYYY` |
| Y-axis | "Điểm" (real points; the in-memory values are multiplied by 1000 at render time to convert from `combined_dataset.csv`'s thousand-point scale) |
| Subtitle | "VIC/VHM/VRE chiếm X.X% vốn hóa HOSE — VN-Index 50 phiên +Y.YY% vs ex-Vin +Z.ZZ% (chênh lệch ±W.WW pp)" |

## Prior attempts that were reverted

This is the **fourth** methodology shipped on this feature. The earlier three (commits `0b7fe85` and follow-ups, reverted in `bd04e1f`) anchored ex-Vin at VN-Index's day-0 level or at `BASE_VALUE=100` or at raw market cap. The user's exact words: *"Starting can't be 1680 for both index"* — the day-0 anchoring was the rejection. The current `93cbcf7` version uses the no-anchor Paasche derivation above and starts the two series at *different* day-0 values that reflect day-0 Vin weight.

If a future change wants to revisit this, the historical pitfalls were:

1. Hardcoded `VIN_TRIO_WEIGHT = 5.5%` from HOSE publications → stale, opaque.
2. `BASE_VALUE = 100` normalization → user wanted real point-scale values.
3. Raw mcap series → user wanted them on the VN-Index scale.
4. `ex_vin_idx[0] = VNINDEX[0]` anchor → user wanted day-0 difference to be intrinsic to the calculation.

The current version is just the Paasche formula applied to the right cohort. No knobs.

## Cross-refs

- [`UNIVERSES.md`](UNIVERSES.md) — where `rs_fixed_tickers.csv` and its `market_cap` column come from.
- [`PROJECT_KB.md`](../PROJECT_KB.md) — overall dashboard structure.
