# Universes

Four ticker-list files exist in the repo. They serve different purposes and should NOT be mixed.

| File | Tracked? | Generator | Size | Used by | Purpose |
|---|---|---|---|---|---|
| **`tickers.csv`** | ✅ git | manual | 100 | `market_breadth.py`, `intraday_breadth.py` | The canonical breadth universe. Both EOD and intraday breadth charts compute "% of stocks above SMA-N" over these 100 tickers. |
| **`rs_fixed_tickers.csv`** | ✅ git (with `!` override) | `rs_universe_generator.py --sync-universe` (manual) + manual additions | 230 | `eod_batch_downloader.py`, `rs_matrix_3T.py`, `pre_breakout.py` | Unified canonical universe for EOD downloads, RS matrix, pre-breakout scanner. |
| **`institutional_universe_3T.csv`** | ✅ git | external scan (uploaded) | 172 | `rs_universe_generator.py` only (drift detector input) | Source-of-truth list of tickers that pass the 3T (≥3 billion VND) average daily-trading-value floor on HOSE/HNX. |
| **`rs_universe.csv`** | ✅ git (legacy) | hand-curated, then orphaned | 206 | nothing — orphaned post-unification | Was the pre-breakout watchlist before unification (commit `bd5363e`). Safe to delete in a follow-up. |
| **`crypto_universe.csv`** | ✅ git (with `!` override) | manually pinned | 50 | `rs_matrix_crypto.py` | Top-50 crypto tickers in yfinance format (`BTC-USD`, `ETH-USD`, …). BTC included for benchmarking only; excluded from rated cohort. |

## The rule that bit us repeatedly: breadth ≠ RS universe

**Breadth (top-100, `tickers.csv`)** is intentionally a smaller, top-cap-weighted reading. The chart's title "Top 100 cổ phiếu vốn hóa lớn HOSE+HNX" is the contract — keep it true. The unified `rs_fixed_tickers.csv` was added later so RS analysis and pre-breakout signals can scan a wider cohort, but the **breadth headline indicator stays at top-100.**

How this is enforced today:
- `intraday_breadth.py:read_top100_tickers()` reads `tickers.csv` directly.
- `market_breadth.py` calls `calculate_breadth(breadth_price_data, …)` after pre-filtering `price_data` to the top-100 — see the comment block at the call site.
- `_build_eod_prices_frame()` in `intraday_breadth.py` filters its source DataFrame to `top100_set` before the rolling-mean computation.

**Verification any time you change breadth-related code:** the intraday chart's last EOD point (T-1) must equal the EOD chart's penultimate column number-for-number. If they diverge, universe drift. See [INTRADAY_BREADTH.md](INTRADAY_BREADTH.md).

## How `rs_fixed_tickers.csv` got to 230

After the universe-unification commit `bd5363e` (2026-05-05):
- 172 institutional 3T names (the auto-scanned baseline).
- + 58 manual additions to broaden RS / pre-breakout coverage. Each carries `lock_rule = "Manual addition for pre_breakout/breadth coverage (unified universe)"` for audit clarity.

The 58 manual additions deliberately don't pass the 3T liquidity floor. The Universe Drift Alert flags them as false-positive removals every day; the dashboard banner is suppressed in `market_breadth.py` for that reason (commit `dc5064a`). The drift script still runs and writes `logs/universe_drift_*.txt` for audit history.

⚠️ **Do not run `python rs_universe_generator.py --sync-universe`** — it would replace `rs_fixed_tickers.csv` with the fresh 172-ticker institutional scan and wipe the 58 manual additions. The daily pipeline runs without that flag.

## When you actually need to widen breadth

If you ever decide breadth should track 200 names instead of 100:
1. Update `tickers.csv` with the new list.
2. Bump the slice limit in both `market_breadth.py:read_tickers()` (`[:100]` × 2 occurrences) and `intraday_breadth.py:TOP_N`.
3. The chart subtitle "Top 100 cổ phiếu vốn hóa lớn HOSE+HNX" needs an edit too.

Don't take the shortcut of pointing breadth code at `rs_fixed_tickers.csv`.
