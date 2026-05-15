# Crypto RS Heatmap

A second Relative Strength heatmap below the VN heatmap, covering a pinned top-50 crypto universe rated against BTC.

## At a glance

- **Universe**: 50 coins pinned in `crypto_universe.csv` (Yahoo-style symbols like `BTC-USD`, `ETH-USD`; mapped to Binance USDT pairs at fetch time).
- **Benchmark**: BTC. Excluded from the rated cohort (it's the denominator).
- **Composite formula**: same 30% RS + 70% momentum blend as the VN heatmap (`rs_matrix_3T.py`). Ratings on the same 1–99 scale, so a `90` means the same thing on both panels.
- **Pipeline stage**: stage 4 of `run_daily_update.py` (between `RS 3T` and `Breadth`).
- **Data source** (since commit `829e32a`, May 2026): **Binance public klines primary, yfinance fallback**.
  - 49 / 50 tickers resolve to a Binance `<TICKER>USDT` pair (`_to_binance_symbol`, `_fetch_binance_klines`). Endpoint: `https://api.binance.com/api/v3/klines?symbol=…&interval=1d&limit=200`. No auth, no rate-limit risk at this scale (Binance allows 1200 req/min/IP).
  - `KAS-USD` is not listed on Binance — falls back to yfinance and inherits its lag.
  - The previous Yahoo-only pipeline was lagging the prior-UTC-day's daily aggregation by hours, so a 06:00–07:30 ICT run would leave the heatmap a full session behind VN RS. Binance publishes the just-closed UTC bar at 00:00 UTC (07:00 ICT), fixing the morning freshness gap.
  - Sync summary log: `Sync summary | binance=N | yfinance_fallback=N | cache_fallback=N`. If `yfinance_fallback` jumps above ~2-3, investigate (delisting on Binance, IP rate-limit, etc.).
- **Cache**: `cache/rs_history_crypto/<ticker>.csv`, persisted to `gs://vn-market-breadth/cache/` like the VN side. Final fallback when both Binance and yfinance fail.

## What you see on the dashboard

Panel below the VN heatmap, titled "Relative Strength Heatmap — Crypto" with tag "Top 50 vs BTC".

Subtitle (italic, gray):
> Cập nhật bảng: HH:MM DD/MM/YYYY (giờ Việt Nam) | Nến mới nhất: UTC DD/MM/YYYY (đóng lúc 07:00 ngày DD/MM/YYYY ICT)

Two pieces of timing info:
- **Cập nhật bảng** — when `rs_matrix_crypto.csv` was last rewritten by the pipeline (file mtime in ICT).
- **Nến mới nhất** — the closed UTC daily candle's date + the corresponding ICT close timestamp (always 07:00 ICT the day after the UTC date).

## The closed-candle rule (important)

Crypto trades 24/7; the daily-bar boundary is **00:00 UTC = 07:00 Asia/Ho_Chi_Minh** for both Binance and yfinance.

When the pipeline runs at 15:15 ICT (08:15 UTC), today's UTC bar is ~8.25 hours into its 24-hour window — an in-progress partial. The script **drops any bar dated today UTC** in both the cache loader and the fetcher (`_drop_in_progress_utc_bar` in `rs_matrix_crypto.py`), so the heatmap's rightmost column is always the candle that closed at 07:00 ICT today, not a mid-day snapshot. Binance returns the in-progress today-UTC bar the same way Yahoo did (close = current intraday), so the same filter still applies.

Implication for the user: at 15:30 ICT on Wednesday, the rightmost column shows **Tuesday UTC** (closed at 07:00 ICT Wed morning).

## Universe management

`crypto_universe.csv` is hand-pinned. Schema:
```
ticker,company_name,exchange,market_cap,industry,locked_at,lock_rule
BTC-USD,Bitcoin,Crypto,,Layer 1,2026-05-05,Pinned crypto top-50 (benchmark)
ETH-USD,Ethereum,Crypto,,Layer 1,2026-05-05,Pinned crypto top-50
...
```

`market_cap` is intentionally blank — we don't auto-rotate. To add/remove coins, edit the CSV directly and commit.

### Coverage after the Binance switch (May 2026)

- **48 / 50 via Binance** (`binance=48` in the sync summary).
- **1 / 50 via yfinance fallback**: `KAS-USD` — not listed on Binance.
- **1 / 50 benchmark** (`BTC-USD` itself, via Binance).

Pre-switch, the old Yahoo-only pipeline silently lost ~10 tickers to symbol-rename and feed instability (`MATIC-USD` → `POL-USD`, `RNDR-USD` → `RENDER-USD`, `UNI-USD/APT-USD/IMX-USD/GRT-USD/SUI-USD/STX-USD/TAO-USD` flaky on Yahoo). Binance has all of them under their canonical `<TICKER>USDT` ticker, so the post-switch matrix builds cleanly over all 49 ratable coins.

To audit live coverage, hit `https://api.binance.com/api/v3/klines?symbol=<TICKER>USDT&interval=1d&limit=2` and confirm 200 OK with 2 rows. HTTP 400 means "not listed", and `incremental_sync_history` will fall back to yfinance for that one ticker.

## Pipeline integration

```
run_daily_update.py
  ├─ Stage 1: eod_batch_downloader.py
  ├─ Stage 2: rs_universe_generator.py
  ├─ Stage 3: rs_matrix_3T.py
  ├─ Stage 4: rs_matrix_crypto.py        ← this
  └─ Stage 5: market_breadth.py          ← reads rs_matrix_crypto.csv via load_crypto_rs_payload()
```

`market_breadth.py:load_crypto_rs_payload()` produces a separate JS-renderable payload; the HTML panel is rendered by inline code in `build_html()` directly under the VN heatmap section.

## Schema of `rs_matrix_crypto.csv`

Identical to `rs_matrix_3T.csv` (same columns, including `rs_rating`, `latest_rs_rating`, `weighted_momentum_score`, `weighted_momentum_rating`). This means the heatmap renderer in `market_breadth.py` works for both with no changes — only the source file path differs.

## Local smoke test

```bash
.venv/Scripts/python.exe rs_matrix_crypto.py
# Logs: 'Crypto RS complete | latest=2026-05-06 | leaders=...'
# Writes rs_matrix_crypto.csv (gitignored, regenerated each run)
# Writes cache/rs_history_crypto/<ticker>.csv per coin
```

## Tunables — `rs_matrix_crypto.py`

| Constant | Value | Meaning |
|---|---|---|
| `BENCHMARK_TICKER` | `BTC-USD` | Excluded from rated cohort |
| `RS_LOOKBACK_CALENDAR_DAYS` | 90 | Same as VN matrix |
| `RS_OUTPUT_SESSIONS` | 20 | Heatmap depth |
| `INITIAL_FETCH_BUFFER_DAYS` | 150 | Initial-fetch span (90 + 60-day buffer for SMA history) |
| `BINANCE_KLINES_URL` | `https://api.binance.com/api/v3/klines` | Primary fetch endpoint |
| `BINANCE_FETCH_LIMIT` | 200 | ~6.5 months of daily bars per request, well over the 90-day RS window |
| `YF_RATE_LIMIT_DELAY` | 0.6s | Pacing between yfinance fallback calls |

Composite blend constants (RS / momentum weights) are shared with `rs_matrix_3T.py` semantics — both use 30/70.
