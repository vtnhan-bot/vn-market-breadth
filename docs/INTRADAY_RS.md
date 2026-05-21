# Intraday Relative Strength Heatmap

A live update to the VN RS heatmap that runs every 15 min during VN trading hours and prepends a `HH:MM`-tagged column to the leftmost position of the table with each ticker's current intraday RS rating. Settled EOD columns to the right of it are untouched.

## At a glance

- **Universe**: 230 tickers in `rs_fixed_tickers.csv`. Same cohort as the daily EOD RS heatmap.
- **Cadence**: every 15 min during VN trading window (09:30–11:30 / 13:00–14:45 ICT), via the existing `intraday-breadth-job` Cloud Run cron (`*/15 9-14 * * 1-5`).
- **Output**: `gs://vn-market-breadth/intraday_rs_3T.json` (tiny — ~13 KB, one row per ticker).
- **Render**: client-side JS in `market_breadth.py`'s output HTML — polls the JSON every 60s, prepends a column whose header is the latest `HH:MM` tick.
- **Source code**: `intraday_rs_3T.py`, called by `intraday_breadth.py:main()` at the end of each successful breadth tick.

## Methodology

For each tick at time `t` during trading hours:

1. **History**: load yesterday's settled `combined_dataset.csv` already on disk (intraday-breadth-job downloaded it from `gs://vn-market-breadth/intraday/`).
2. **Current prices**: single `Trading.price_board()` batch call returns the 230 tickers' last match prices in raw VND. Divided by `PRICE_DIVISOR=1000.0` to match `combined_dataset.csv`'s thousand-VND scale.
3. **Per ticker**: substitute today's intraday price as `current_close` and run the same `calculate_return_90d` and `calculate_weighted_momentum_score` math as `rs_matrix_3T.py`.
4. **Cross-section rank**: rank `stock_return_90d` and `weighted_momentum_score` across the 230-ticker cohort. Blend 30/70. Clip to 1–99.

### Math note: VNINDEX is skipped during intraday

The settled EOD pipeline computes `relative_performance = stock_return_90d − index_return_90d` and then ranks across the cohort. The index term is a constant for every row on a given day, so subtracting it is identity for the percentile rank. The intraday script ranks `stock_return_90d` directly and produces the same `rs_rating` with one fewer data dependency (no need to fetch live VNINDEX). Verified algebraically in `intraday_rs_3T.py`'s docstring.

## JSON schema (`gs://vn-market-breadth/intraday_rs_3T.json`)

```jsonc
{
  "session_date": "2026-05-22",
  "tick_time_ict": "10:00",
  "last_updated_ict": "10:00 22/05/2026",
  "rows": [
    {"ticker": "VNM", "rs_rating": 87, "daily_change_pct": 1.23},
    {"ticker": "HPG", "rs_rating": 64, "daily_change_pct": -0.40},
    ...
  ]
}
```

- `rs_rating` is `null` when there's insufficient history (rare — only for tickers with <61 sessions).
- `daily_change_pct` = `(intraday_price / ref_price − 1) × 100`, where `ref_price` is yesterday's settled close from the same `price_board()` response.

## Dashboard JS contract

Two helpers + a fetch loop in `market_breadth.py`'s `<script>` block:

```javascript
function todayIsoIct() { /* YYYY-MM-DD in Asia/Ho_Chi_Minh */ }
function todayDdMmIct() { /* DD-MM in Asia/Ho_Chi_Minh */ }
function removeIntradayDom(table) { /* strip any previously-inserted intraday th + td cells */ }
function applyIntradayRs(doc) {
  // 1. Guard: if heatmap's leftmost EOD column header == today's DD-MM,
  //    the EOD pipeline has caught up. removeIntradayDom and return.
  // 2. If doc.session_date != todayIsoIct(), leave the table alone.
  // 3. Otherwise prepend (or replace) a th + td per row with HH:MM header
  //    and per-ticker rs_rating + daily_change_pct values.
}

async function pollIntradayRs() { /* fetch + applyIntradayRs */ }
pollIntradayRs();
setInterval(pollIntradayRs, 60 * 1000);
```

Idempotent: re-running rewrites the same intraday cell (matched by `data-intraday="1"` attribute) rather than accumulating columns.

## Behavior across the trading day (ICT)

| Time window | Heatmap leftmost column |
|---|---|
| 09:30 → 14:45 (live cron) | Latest `HH:MM` intraday tick, prepended ahead of the EOD columns. |
| 14:45 → 15:15 (post-close gap) | Still the 14:30 (last) intraday tick. Cron isn't firing but the JSON hasn't been cleared. |
| **15:15 onward (EOD pipeline finished)** | The EOD pipeline regenerates the HTML with today's settled date as the leftmost EOD column. The JS guard sees `leftmost_eod_header == today_dd_mm` and *removes* the intraday column. From 15:15+ ICT only the settled heatmap is visible. |
| Next day 09:30 | New intraday cycle. The JSON's `session_date` becomes today, the guard's `eodTh.textContent` is now yesterday's DD-MM, the prepend resumes. |

This is the user-chosen behavior (Option 2: hide intraday once EOD has caught up). Commit `fe4d7e9` ships the guard; `fe10b4e` is a follow-up hotfix for a duplicate `const headerRow` that briefly took the whole `<script>` block offline.

## How `intraday-breadth-job` hosts both computations

The intraday Cloud Run job runs `python3 intraday_breadth.py`. At the end of `intraday_breadth.main()`, after `update_intraday_json_on_gcs(...)` finishes, the script imports `intraday_rs_3T` and calls `run_intraday_rs(now_ict, combined_local)`. The call is wrapped in try/except so a failure in the RS step never blocks the breadth tick:

```python
try:
    from intraday_rs_3T import run_intraday_rs
    run_intraday_rs(now_ict, combined_local)
except Exception as exc:
    LOGGER.warning("Intraday RS step crashed (non-fatal): %s", exc)
```

Same image, same cron, same SA, same Pub/Sub — no new infrastructure for the RS layer.

## Operational caveats

**Dockerfile COPY list**: `intraday_rs_3T.py` must be in the explicit `COPY` block. If you remove it, the next cron tick logs `ModuleNotFoundError: No module named 'intraday_rs_3T'` and the breadth tick still writes (just without the RS update). See [OPERATIONS.md](OPERATIONS.md).

**Cache busting**: the JSON is written with `cache_control = no-cache, no-store, must-revalidate`. The JS appends `?_=${Date.now()}` on every fetch.

**Yfinance vs Binance vs vnstock**: RS heatmap (VN) uses vnstock for intraday prices via `Trading.price_board()`. Crypto RS (separate file `intraday_rs_crypto.json` not implemented) would need its own source — Binance klines as in [CRYPTO_RS_HEATMAP.md](CRYPTO_RS_HEATMAP.md).

## Smoke test

```bash
# Local dry-run (uses an existing combined_dataset.csv, hits real vnstock, skips GCS upload)
INTRADAY_LOCAL_COMBINED=data/2026-05-22/combined_dataset.csv \
INTRADAY_DRY_RUN=1 \
.venv/Scripts/python.exe intraday_rs_3T.py
# Logs: '230 tickers loaded', '230 prices fetched', 'DRY_RUN — would publish intraday RS with 230 rows at HH:MM'
```

## Cross-refs

- [`INTRADAY_BREADTH.md`](INTRADAY_BREADTH.md) — the breadth half of the same `intraday-breadth-job` cron.
- [`RS_AND_PREBREAKOUT.md`](RS_AND_PREBREAKOUT.md) — the EOD RS Rating formula (30/70 blend) that intraday mirrors.
- [`OPERATIONS.md`](OPERATIONS.md) — Dockerfile COPY-list gotcha, image-digest pinning.
