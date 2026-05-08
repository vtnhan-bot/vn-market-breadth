# Composite RS Rating + Pre-breakout Signal Engine

The dashboard's RS Heatmap and Pre-breakout panel both rely on a single composite metric: **`rs_rating`** (1–99 scale), built daily by `rs_matrix_3T.py`. This doc covers the formula and how the pre-breakout layers consume it.

## Data flow (May 2026 — post cache-removal refactor)

`rs_matrix_3T.py` reads each ticker's daily history **directly from `data/<today>/combined_dataset.csv`** — the file `eod_batch_downloader.py` just wrote with vnstock's freshly back-adjusted full series (~420 calendar-day window, all 230+ tickers). There is no per-ticker history cache. This means:

- Corporate-action back-adjustments by vnstock propagate end-to-end automatically (downloader → combined_dataset → matrix).
- RS 3T stage runtime: ~13 seconds (was ~6 minutes when it did 230 incremental vnstock fetches).
- `cache/rs_history/` exists on GCS only as orphaned legacy files — no code consumes them.

If you're debugging an unexpected RS rating: pull `gs://vn-market-breadth/intraday/combined_dataset.csv`, slice to the ticker, check the close history. Whatever's there is what the matrix saw.

## The composite RS Rating formula

For each (ticker, session_date) cell in `rs_matrix_3T.csv`:

### Component 1 — Pure RS percentile (`rs_pct`)

90-day relative-performance vs VNINDEX, ranked cross-sectionally per session:
```
stock_return_90d   = close[D] / close[D − 90 calendar days] − 1
index_return_90d   = vnindex[D] / vnindex[D − 90 calendar days] − 1
relative_performance = stock_return_90d − index_return_90d
rs_pct             = rank_pct(relative_performance) per session   ∈ [0, 1]
```

### Component 2 — Weighted-momentum percentile (`weighted_momentum_pct`)

50/30/20 weighted blend of 10/20/60-session momentum, ranked cross-sectionally:
```
weighted_ratio          = 0.50·(close/close_10) + 0.30·(close/close_20) + 0.20·(close/close_60)
weighted_momentum_score = (weighted_ratio − 1) × 100
weighted_momentum_pct   = rank_pct(weighted_momentum_score) per session   ∈ [0, 1]
```

### Composite — `rs_pct_blended`

```
rs_pct_blended = 0.30 × rs_pct + 0.70 × weighted_momentum_pct
```

History: started 50/50; tuned to 30/70 to favour short-term trend strength (the user's preference for the way they read the dashboard).

### Final rating — `rs_rating`

```
rs_rating = round(rs_pct_blended × 98 + 1)   clipped to [1, 99]
```

Scale convention: `99` is the top-percentile leader of the universe; `1` is the bottom. A rating of `90` means "top 10% of universe."

## Universe used by the matrix

`rs_fixed_tickers.csv` — 230 tickers (172 institutional 3T + 58 manual additions for breadth/pre-breakout coverage). NOT the top-100 breadth universe. See [UNIVERSES.md](UNIVERSES.md).

The matrix output (`rs_matrix_3T.csv`) is consumed by:
- The "Relative Strength Heatmap (Institutional 3T)" panel in `market_breadth.py`.
- `pre_breakout.py` — gates both signal layers on `rs_rating`.

## Pre-breakout signal engine — `pre_breakout.py`

Two layers, both gated on the composite `rs_rating` from above (replacing the older Mansfield ratio + RS-line constructions).

### Layer A — RS leader still in base

```
rs_rating[T-1]                          ≥ 90        # elite, top 10%
AND  close[T-1] / close.rolling(252).max()  ≤ 0.95   # still ≤ 95% of 252-day high (in base)
```

Reading: composite RS is in the top 10% of the universe, but the price hasn't broken out yet — classic pre-breakout setup.

### Layer B — RS leader with Bollinger squeeze

```
rs_rating[T-1]                                 ≥ 90
AND  BB(20, 2σ) width.percentile(126 days)     ≤ 20  # bottom-20% of trailing distribution
```

Reading: composite RS is elite, and Bollinger Band width is in the tightest 20% of the last six months — squeeze about to release.

### Watch lists

Same structural conditions, but `rs_rating ≥ 80` (top 20%, "leading") and Layer-B's BB %ile relaxed to ≤ 40. Top-10 candidates closest to triggering.

### Tunable constants — `pre_breakout.py`

| Constant | Value | Meaning |
|---|---|---|
| `RS_RATING_TRIGGER` | 90 | Strict trigger threshold (top 10%) |
| `RS_RATING_WATCH` | 80 | Watch-list threshold (top 20%) |
| `WINDOW_52W` | 252 | Rolling-max window for "in base" check |
| `PRICE_BASE_MAX` | 0.95 | "In base" if price ≤ 95% of rolling max |
| `BB_PERIOD`, `BB_K` | 20, 2.0 | Bollinger-band parameters |
| `BB_PCTILE_HIST` | 126 | Trailing window for BB-width percentile |
| `SQUEEZE_PCTILE` | 20.0 | Trigger threshold (bottom 20% of trailing widths) |
| `SQUEEZE_PCTILE_WATCH` | 40.0 | Watch threshold (bottom 40%) |

## HTML rendering — `_patch_pre_breakout.py`

Tables show: Ticker, Close, **RS Rating** (bold), and either `Δ vs 52w high` (Layer A) or `BB Width / BB %ile` (Layer B). The "⭐ Cả 2 tín hiệu cùng kích hoạt" highlight at the top groups any ticker that triggers both layers — the strongest configuration.

Methodology blurb at the bottom of the panel explains the formula for the user.

## Coverage and known gaps

- The pre-breakout universe is `rs_fixed_tickers.csv` (230 tickers).
- Tickers without a composite rating (recent IPOs lacking 60+ session history for the momentum component, or bars insufficient for the 90-day RS calc) are excluded silently. Currently 4 such tickers: CRV, GEL, HPA, LGC.
- The meta panel reports `Phân tích X/Y mã RS (thiếu OHLC: M | thiếu RS Rating: N)` so coverage is transparent.

## Local recompute / smoke test

```python
import pre_breakout
from pathlib import Path
result = pre_breakout.compute(
    Path("data/2026-05-07/combined_dataset.csv"),
    Path("rs_fixed_tickers.csv"),
    Path("rs_matrix_3T.csv"),
)
print(f"layer_a triggered: {len(result.layer_a)}, both: {len(result.both)}")
print(f"missing_ohlc={result.meta['missing_ohlc_count']}, missing_rating={result.meta['missing_rating_count']}")
```
