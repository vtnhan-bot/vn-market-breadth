# VN Market Breadth Dashboard — Project Knowledge Base

> **⚠ GCP note (updated 2026-06-21):** This doc is partly stale. Current reality for this engine:
> - The `market-breadth-job` and `intraday-breadth-job` Cloud Run jobs are triggered by **VM systemd timers** (`engine-market-breadth.timer` / `engine-intraday-breadth.timer` on the pattern-engine VM), **NOT Cloud Scheduler**. Any "Cloud Scheduler" trigger, the "Cloud Scheduler → Cloud Run Job" arch diagram, and the "Did Cloud Scheduler fire?" debug step below describe deleted infrastructure.
> - Crypto market data uses **KuCoin**, NOT Binance. Ignore "crypto via Binance" references below.
> - **Do NOT run the Cloud Scheduler steps below — they would recreate deleted jobs and incur cost.**
> - Canonical current state: this project's CLAUDE.md → "GCP Deployment & Cost Safety", and d:\Claude\Devops\ARCHITECTURE.md. Content below is kept for reference.

> **Purpose**: a single document that lets a senior engineer (or a future Claude session) get from cold-start to confidently shipping changes in under 30 minutes. Read this first; it points you at everything else.
>
> **Last refresh**: 2026-06-01 (after May 17–22 shipment: DXY 150-session chart, intraday RS heatmap with HH:MM column + EOD-catch-up guard, VN-Index ex-Vingroup line chart; plus the May 2026 cost analysis — May bill ~62K VND, Artifact Registry cleanup policy keep-last-5 + delete-older-than-7d applied 2026-06-01, AR storage drop expected within ~24h).
>
> **Topic deep-dives** in [`docs/`](docs/):
> - [`docs/INTRADAY_BREADTH.md`](docs/INTRADAY_BREADTH.md) — the live 15-min breadth chart (architecture, data contract, JS polling, Đóng cửa rollover, intraday-RS sibling hook).
> - [`docs/INTRADAY_RS.md`](docs/INTRADAY_RS.md) — the live 15-min RS heatmap update for 230 VN tickers (HH:MM column, post-EOD JS guard, vnstock price_board source).
> - [`docs/RS_AND_PREBREAKOUT.md`](docs/RS_AND_PREBREAKOUT.md) — composite RS Rating formula + pre-breakout signal layers.
> - [`docs/CRYPTO_RS_HEATMAP.md`](docs/CRYPTO_RS_HEATMAP.md) — top-50 crypto vs BTC heatmap, Binance-primary + yfinance-fallback, UTC-vs-ICT timing.
> - [`docs/VNINDEX_EX_VIN.md`](docs/VNINDEX_EX_VIN.md) — VN-Index excluding VIC/VHM/VRE; Paasche-formula derivation, mcap proxy, ±0.01 calibration check.
> - [`docs/UNIVERSES.md`](docs/UNIVERSES.md) — which ticker file is used where, and why breadth ≠ RS universe.
> - [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — three schedules, freshness branching, manual triggers, image refresh, Dockerfile-COPY trap, common diagnostic recipes.
> - [`docs/COST_PROTECTION.md`](docs/COST_PROTECTION.md) — budget alerts + 100% auto-killswitch + 80% Telegram alert.

---

## 1. At a glance

| Item | Value |
|---|---|
| One-liner | Daily VN-equity dashboard + intraday breadth + US macro (VIX, Nasdaq) + pre-breakout scanner + crypto RS heatmap. |
| Live URL | https://storage.googleapis.com/vn-market-breadth/index.html |
| Daily refresh | Weekdays 15:15 ICT (after VN market close at 14:45) — full pipeline rebuild, finishes ~15:28 |
| Intraday refresh | Weekdays every 15 min during 09:30–11:30 / 13:00–14:45 ICT — breadth panel only |
| Morning refresh | Tue–Sat 07:30 ICT — pulls fresh US EOD (VIX/Nasdaq) and fresh UTC crypto daily bar (Binance) before VN session opens. See `docs/OPERATIONS.md`. |
| Repo | https://github.com/vtnhan-bot/vn-market-breadth (master) |
| Local working dir | `D:\Claude\Market on website` (Windows) |
| GCP project | `project-feb6df0e-9749-4925-b4e` (region `asia-southeast1`) |
| GCS bucket | `vn-market-breadth` |
| Cloud Run jobs | `market-breadth-job` (full pipeline, runs at both 07:30 ICT and 15:15 ICT cron schedules), `intraday-breadth-job` (every 15 min during VN trading hours) |
| Cloud Scheduler | `intraday-breadth-schedule` `*/15 9-14 * * 1-5` · `market-breadth-schedule` `15 15 * * 1-5` · `market-breadth-us-close` `30 7 * * 2-6` — all Asia/Ho_Chi_Minh |
| Cost protection | `Market dashboard 120000 VND cap` budget on billing acct `017EA5-270660-A8352F`; `billing-killswitch` Cloud Function unlinks billing at 100%; `telegram-budget-alert` Cloud Function pings `@SuperGemini_bot` at 80%. See `docs/COST_PROTECTION.md`. |
| Primary user | CTO / portfolio manager — visits the URL daily, makes trade decisions from it |

---

## 2. What the dashboard contains (top to bottom)

The page (`market_breadth.html`) is built by `market_breadth.py` and uploaded to GCS as `index.html`.

1. **EOD Breadth chart** — % of top-100 HOSE+HNX stocks above SMA-3/5/10/20/50/200 over the last 50 sessions. Six lines, line widths/dashes per the shared color scheme. The headline indicator. Universe = `tickers.csv`.
2. **📡 Intraday breadth chart** *(new — May 2026)* — same 6 MA periods, same top-100 universe, same color/style as the EOD chart. Shows 49 EOD days ending T-1 + 1 live intraday point at the rightmost, refreshed every 15 min during VN trading hours via JS polling of `gs://vn-market-breadth/intraday_breadth.json`. See [`docs/INTRADAY_BREADTH.md`](docs/INTRADAY_BREADTH.md).
3. **VNINDEX 50-session candlestick** with volume.
4. **VN-Index vs VN-Index loại VIC/VHM/VRE — 50 phiên** *(new — May 2026)* — two-line chart, both computed by the same Paasche formula `ex_vin_index[t] = VNINDEX[t] × (ex_vin_mcap[t] / total_mcap[t])`. Day-0 starting values differ by Vin trio's day-0 weight in HOSE; no anchor fudge. Subtitle reports today's Vin-trio HOSE share and the 50-session % spread. See [`docs/VNINDEX_EX_VIN.md`](docs/VNINDEX_EX_VIN.md).
5. **CBOE VIX — 100 phiên** candlestick (yfinance `^VIX`).
6. **Nasdaq Composite — 100 phiên** candlestick (yfinance `^IXIC`).
7. **US Dollar Index (DXY) — 150 phiên** *(new — May 2026)* candlestick (yfinance `DX-Y.NYB`). No volume pane — DXY is an index, not tradable. Same partial-bar drop logic as VIX/Nasdaq (`_load_us_index_data` excludes the in-progress US trading-day row when fetched during NYSE hours).
8. **Breadth detail tables** — current SMA-N readings + day/week deltas, and a composite gauge with a Vietnamese-language verdict ("THẬN TRỌNG" / "TÍCH CỰC" / etc.).
9. **🚀 Pre-breakout panel** — two-layer scanner over the unified RS universe (`rs_fixed_tickers.csv`, ~230 names), gated on the composite RS Rating from the matrix:
   - **Layer A**: `rs_rating ≥ 90` AND `price ≤ 95% of 252-d high` (in base).
   - **Layer B**: `rs_rating ≥ 90` AND BB(20, 2σ) width in bottom 20% of trailing 126-session distribution (squeeze).
   - Watch lists relax to `rs_rating ≥ 80`.
   - Highlighted "⭐ Both" block at the top when a ticker passes both layers.
   - See [`docs/RS_AND_PREBREAKOUT.md`](docs/RS_AND_PREBREAKOUT.md) for the rs_rating formula.
10. **Relative Strength Heatmap (Institutional 3T)** — composite RS Rating (1–99) per ticker per session, last 20 sessions × ~230 tickers. Composite blend = **30% RS + 70% momentum** (changed from 50/50 in May 2026). During VN trading hours an extra leftmost column tagged `HH:MM` is prepended client-side with each ticker's intraday RS; the column auto-removes when the 15:15 ICT EOD pipeline catches up and today's settled date becomes the leftmost EOD column. See [`docs/INTRADAY_RS.md`](docs/INTRADAY_RS.md).
11. **Relative Strength Heatmap — Crypto** — top-50 cryptos vs BTC, same composite formula, displayed below the VN heatmap. Closed-candle convention: rightmost column is always the UTC daily bar that closed at 07:00 ICT today, never an in-progress partial. Binance klines primary, yfinance fallback for KAS-USD. See [`docs/CRYPTO_RS_HEATMAP.md`](docs/CRYPTO_RS_HEATMAP.md).

---

## 3. Architecture

### 3.1 Local development (Windows)

```
D:\Claude\Market on website\
├── *.py          ← pipeline scripts (see §4)
├── *.csv         ← input universes (institutional, fixed, RS, top-100)
├── data/         ← runtime: per-day OHLC dumps  (gitignored)
├── cache/        ← runtime: vnstock per-ticker history caches  (gitignored)
├── logs/         ← runtime: daily run logs  (gitignored)
├── audit_logs/   ← runtime: tamper-evident audit  (gitignored)
└── .env          ← VNSTOCK_API_KEY etc. (gitignored)
```

The user runs `run_daily_update.py` (or `launch.bat`) when they want a local refresh. The same script is what the Cloud Run container invokes.

### 3.2 Cloud production (GCP)

```
GitHub master ── push ──▶ GitHub Actions (.github/workflows/update_chart.yml)
                              │
                              ▼
                         Cloud Build (asia-southeast1)
                              │ docker push :latest
                              ▼
                Artifact Registry (market-repo/market-breadth:latest)
                              │
                              ▼  (next scheduled run pulls :latest)
Cloud Scheduler ─ 15:30 ICT ─▶ Cloud Run Job (market-breadth-job)
                                  │
                                  ├── reads VNSTOCK_API_KEY from Secret Manager
                                  ├── restores cache/ from gs://vn-market-breadth/cache/
                                  ├── runs run_daily_update.py (4-stage pipeline)
                                  ├── uploads market_breadth.html → gs://…/index.html
                                  └── persists cache/ back to gs://…/cache/
```

The public URL is just `gs://vn-market-breadth/index.html` exposed via GCS public read. No Cloud CDN, no Load Balancer — minimum infra.

---

## 4. The daily pipeline (`run_daily_update.py`)

Five sequential stages. Each is a standalone script that can be run individually for local debugging.

| # | Script | What it does | Reads | Writes |
|---|---|---|---|---|
| 1 | `eod_batch_downloader.py` | Fetches today's EOD bar for the unified universe (`rs_fixed_tickers.csv` ≈ 230 tickers) via vnstock with rate-limit pacing. | `rs_fixed_tickers.csv`, `cache/*.pkl` | `data/<today>/*.csv`, `data/<today>/combined_dataset.csv` |
| 2 | `rs_universe_generator.py` | Drift detector — diffs `institutional_universe_3T.csv` (172) vs `rs_fixed_tickers.csv` (230). Reports only; never modifies the locked universe (would wipe the 58 manual additions). | `institutional_universe_3T.csv`, `rs_fixed_tickers.csv` | `logs/universe_drift_*.txt` |
| 3 | `rs_matrix_3T.py` | Composite RS Rating (1–99): 30% relative-performance percentile + 70% weighted-momentum percentile, last 20 sessions × ~230 tickers. **Reads OHLC directly from `data/<today>/combined_dataset.csv`** (no per-ticker cache; corporate-action back-adjustments propagate automatically). | `rs_fixed_tickers.csv`, `data/<today>/combined_dataset.csv` | `rs_matrix_3T.csv` |
| 4 | `rs_matrix_crypto.py` *(new May 2026)* | Same composite formula, but for top-50 cryptos vs BTC. Drops in-progress UTC daily bar so rightmost is always the candle that closed at 07:00 ICT. | `crypto_universe.csv`, `cache/rs_history_crypto/*.csv` | `rs_matrix_crypto.csv` |
| 5 | `market_breadth.py` | Builds HTML: EOD breadth chart (top-100 only), VNINDEX/VIX/Nasdaq candlesticks, breadth detail tables, RS heatmap (VN), RS heatmap (Crypto), pre-breakout panel via `_patch_pre_breakout`, intraday-chart container + JS poller. | `data/<today>/combined_dataset.csv`, `rs_matrix_3T.csv`, `rs_matrix_crypto.csv`, `rs_fixed_tickers.csv`, `tickers.csv` | `market_breadth.html` |

A successful run produces a single self-contained `market_breadth.html` (~750 KB) and persists `combined_dataset.csv` to `gs://vn-market-breadth/intraday/combined_dataset.csv` for the intraday job to consume.

### Intraday pipeline (separate Cloud Run job)

`intraday_breadth.py` runs every 15 min during VN trading hours via `intraday-breadth-schedule`. It reads the latest `combined_dataset.csv` from GCS, fetches live prices via `vnstock.Trading.price_board()`, computes `% top-100 above SMA-N` (SMA built from N closed days ending T-1), and writes to `gs://vn-market-breadth/intraday_breadth.json`. The dashboard's intraday panel polls this JSON every 60s. Full design in [`docs/INTRADAY_BREADTH.md`](docs/INTRADAY_BREADTH.md).

### Freshness gate
`market_breadth.py` will **abort with `"CRITICAL: EOD Data Not Fresh"`** if `combined_dataset.csv` was modified before **15:30 ICT today**. This is intentional — guarantees no run ships yesterday's prices as today's. The scheduler is therefore set to fire at 15:30 ICT exactly so the downloader writes fresh data right before the gate check.

### Pre-breakout integration
At the very end of `market_breadth.py main()`, after `build_html()` writes the HTML, the script imports `_patch_pre_breakout.build_panel` and surgically injects the pre-breakout panel HTML + CSS into the file. Wrapped in `try/except` so a missing `pre_breakout` module never breaks the breadth pipeline.

---

## 5. Cloud infrastructure reference

### 5.1 Cloud Run Jobs

```
market-breadth-job            (daily full pipeline)
  Region:        asia-southeast1
  Image:         …/market-breadth:latest
  Resources:     cpu=2, memory=2Gi, task-timeout=15m, max-retries=1
  Command:       (default — bash entrypoint.sh runs run_daily_update.py)
  Env: GITHUB_ACTIONS=true, VNSTOCK_API_KEY (Secret Manager)

intraday-breadth-job          (15-min intraday tick)
  Region:        asia-southeast1
  Image:         …/market-breadth:latest  (same image, different command)
  Resources:     cpu=1, memory=512Mi, task-timeout=120s, max-retries=1
  Command:       python3 intraday_breadth.py
  Env: GITHUB_ACTIONS=true, VNSTOCK_API_KEY (Secret Manager)
```

Both jobs are pinned to the `:latest` tag. The GHA workflow's two `gcloud run jobs update` steps (one per job) re-resolve the digest after every Cloud Build so new code reaches both jobs automatically.

### 5.2 Cloud Scheduler

```
market-breadth-schedule       (daily)
  Cron:          15 15 * * 1-5     (15:15 ICT, weekdays)
  Timezone:      Asia/Ho_Chi_Minh
  Target:        POST  …/jobs/market-breadth-job:run
  Auth:          OAuth (github-deploy@…)
  Deadline:      180s

intraday-breadth-schedule     (15-min intraday)
  Cron:          */15 9-14 * * 1-5    (every 15 min, 09:00–14:59 ICT, weekdays)
  Timezone:      Asia/Ho_Chi_Minh
  Target:        POST  …/jobs/intraday-breadth-job:run
  Auth:          OAuth (github-deploy@…)
  Deadline:      180s
```

The 09:00 / 09:15 / 11:45 / 12:00 / 12:15 / 12:30 / 12:45 fires hit the cron range but the script's own time-window check (09:30–11:30 / 13:00–14:45 ICT) no-ops them silently. Net: 17 actual compute ticks per trading day.

### 5.3 Secret Manager

```
Secret name:  vnstock-api-key
Created:      2026-04-29
Versions:     1 (enabled)
Payload:      40-character vnstock community-tier API key (lifts limit from 20 → 60 req/min)
Access:       roles/secretmanager.secretAccessor granted to 746287134716-compute@developer.gserviceaccount.com
```

### 5.4 GCS bucket layout

```
gs://vn-market-breadth/
├── index.html                       ← public dashboard (no-cache headers)
├── intraday_breadth.json            ← live JSON the dashboard polls (no-cache headers)
├── intraday/
│   ├── combined_dataset.csv         ← daily-pipeline-persisted EOD CSV; intraday job reads this
│   ├── rs_matrix_3T.csv             ← daily-pipeline-persisted RS matrix; for local re-renders
│   └── rs_matrix_crypto.csv         ← daily-pipeline-persisted crypto RS matrix; for local re-renders
└── cache/                           ← incremental-fetch cache, restored at job start, persisted at job end
    ├── *.pkl                        ← per-ticker breadth pickles
    ├── rs_history/*.csv             ← per-ticker history for VN RS matrix (~234 tickers)
    ├── rs_history_crypto/*.csv      ← per-coin history for crypto RS matrix (~50 coins)
    └── rs_company_overview_cache.csv
```

### 5.5 GitHub Actions

```
Workflow:  .github/workflows/update_chart.yml
Triggers:  push to master, manual dispatch
Steps:
  1. Auth to GCP via Workload Identity Federation
  2. gcloud builds submit              →  pushes :latest to Artifact Registry
  3. gcloud run jobs update market-breadth-job   --image …:latest
  4. gcloud run jobs update intraday-breadth-job --image …:latest
```

The workflow occasionally reports false-failure on the `gcloud builds submit` step due to async log streaming — verify by checking `gcloud builds list --region=asia-southeast1`. If the build itself shows `SUCCESS`, the deployment is fine.

If GHA fails to fire after a push (rare but observed once), the workaround is `gcloud builds submit` directly; see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

---

## 6. Operational playbooks

### 6.1 Deploy a code change
```bash
git add <files>
git commit -m "..."
git push origin master
# GitHub Actions auto-rebuilds Docker image (~2-3 min)
# Next scheduled run (or manual trigger) pulls the new :latest image
```

### 6.2 Trigger a manual run (after 15:30 ICT — earlier will fail freshness gate)
```bash
gcloud run jobs execute market-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e \
  --region=asia-southeast1 --wait
```

### 6.3 Tail logs from the latest execution
```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="market-breadth-job"' \
  --project=project-feb6df0e-9749-4925-b4e --limit=200 --order=desc
```

### 6.4 Change the schedule
```bash
gcloud scheduler jobs update http market-breadth-schedule \
  --project=project-feb6df0e-9749-4925-b4e \
  --location=asia-southeast1 \
  --schedule='<cron>' \
  --time-zone='Asia/Ho_Chi_Minh'
```
Cron is interpreted in the `--time-zone` you set, **not** UTC. Verify with `gcloud scheduler jobs describe market-breadth-schedule --location=asia-southeast1`.

### 6.5 Rotate the vnstock API key
```bash
# Add a new version (old version remains usable as a fallback)
echo -n '<new_key>' | gcloud secrets versions add vnstock-api-key --data-file=-
# Cloud Run mounts :latest, so the next execution picks up the new version automatically.
# To disable old versions:
gcloud secrets versions disable <version> --secret=vnstock-api-key
```

### 6.6 Bootstrap a new dev environment
```bash
git clone https://github.com/vtnhan-bot/vn-market-breadth
cd vn-market-breadth
pip install -r requirements.txt
# Drop your VNSTOCK_API_KEY into .env (project root) — gitignored
python run_daily_update.py
```

### 6.7 Debugging "Last-Modified is stale" on the public URL
1. Did Cloud Scheduler fire? → `gcloud scheduler jobs describe market-breadth-schedule --location=asia-southeast1` (check `lastAttemptTime` and `state`).
2. Did the Cloud Run Job succeed? → `gcloud run jobs executions list --job=market-breadth-job --region=asia-southeast1 --limit=5`.
3. If the job failed — pull logs (§6.3) and look for: `EOD Data Not Fresh` (freshness gate), `rate limit` (vnstock guest-tier hit because `VNSTOCK_API_KEY` not bound), or `Task timeout` (bump `--task-timeout`).

---

## 7. Configuration reference (the "magic numbers")

### Breadth (top-100 universe)

| Setting | Value | Where | Why |
|---|---|---|---|
| `SESSIONS_SHOW` (EOD breadth window) | 50 | `market_breadth.py` | Headline chart length |
| `MA_PERIODS` | `[3, 5, 10, 20, 50, 200]` | shared | mbz lines on both EOD + intraday charts |
| `MA_COLORS` | cyan/orange/green/purple/black/red | `market_breadth.py:50` | mbz03/05 dotted, mbz50 width 4 (bold black), others width 2 |
| `EOD_HISTORY_SESSIONS` (intraday chart) | 49 | `intraday_breadth.py` | 49 EOD + 1 latest intraday point = 50 total chart points |
| `TOP_N` (intraday breadth universe) | 100 | `intraday_breadth.py` | Top-100 from `tickers.csv` — same as EOD chart |
| `MIN_OBS` | 10 | `intraday_breadth.py` | Tickers with <10 daily obs excluded (matches EOD `calculate_breadth`) |
| Trading-window | 09:30–11:30 / 13:00–14:45 ICT | `intraday_breadth.py` | VN session hours minus ATO/ATC/lunch |
| `freshness_cutoff` | 15:00 ICT | `market_breadth.py` | Aborts daily run if `combined_dataset.csv` older than this; matches 15:15 schedule |

### RS / Pre-breakout (~230-ticker unified universe)

| Setting | Value | Where | Why |
|---|---|---|---|
| RS composite blend | 30% RS + 70% momentum | `rs_matrix_3T.py:243-244` | Tuned May 2026 from 50/50 |
| `RS_LOOKBACK_CALENDAR_DAYS` | 90 | `rs_source2.py` | Pure-RS lookback |
| `RS_RATING_TRIGGER` | 90 | `pre_breakout.py` | Strict pre-breakout trigger (top 10%) |
| `RS_RATING_WATCH` | 80 | `pre_breakout.py` | Watch-list threshold (top 20%) |
| `WINDOW_52W` | 252 | `pre_breakout.py` | Layer A rolling-max window |
| `PRICE_BASE_MAX` | 0.95 | `pre_breakout.py` | "In a base" if price ≤ 95% of 252-d high |
| `BB_PERIOD`, `BB_K` | 20, 2.0 | `pre_breakout.py` | Bollinger Bands |
| `BB_PCTILE_HIST` | 126 | `pre_breakout.py` | Trailing distribution window for BB squeeze |
| `SQUEEZE_PCTILE`, `SQUEEZE_PCTILE_WATCH` | 20.0, 40.0 | `pre_breakout.py` | Trigger/watch BB-width percentile |

### EOD downloader / Crypto

| Setting | Value | Where | Why |
|---|---|---|---|
| `API_CALL_DELAY_SECONDS` | 1.1 | `eod_batch_downloader.py` | Stays under 60 req/min (community tier) |
| `API_SOURCES` | `["KBS", "VCI", "MSN", "FMP"]` | `eod_batch_downloader.py` | vnstock 3.5+ supported sources |
| `BENCHMARK_TICKER` (crypto) | `BTC-USD` | `rs_matrix_crypto.py` | Excluded from rated cohort |
| `YF_RATE_LIMIT_DELAY` | 0.6s | `rs_matrix_crypto.py` | Per-coin yfinance pacing |
| `_drop_in_progress_utc_bar` | active | `rs_matrix_crypto.py` | Drops today's UTC partial bar so latest = closed candle |

### Cloud Run

| Setting | Value | Why |
|---|---|---|
| `market-breadth-job` task-timeout | 15 min | Buffer for full pipeline + cache cold-start |
| `market-breadth-job` memory | 2Gi | pandas + RS calcs comfortably fit |
| `intraday-breadth-job` task-timeout | 120s | Single-batch price fetch + breadth compute < 30s; buffer is generous |
| `intraday-breadth-job` memory | 512Mi | Smaller workload — only top-100 + EOD CSV |

---

## 8. Recent changelog

### May 2026

| Date | Change | Commit |
|---|---|---|
| 2026-05-08 | RS matrix: read history directly from `combined_dataset.csv` (eliminate `cache/rs_history/`). Corporate-action back-adjustments by vnstock now propagate automatically. RS 3T stage runtime: 6 min → 13 sec. | `4a6b13a` |
| 2026-05-08 | RS matrix: cache-validation guard against back-adjustment drift (intermediate fix; superseded by `4a6b13a`). Caught GEX (drift 45%) and GEE (75%) on first run. | `ed95c58` |
| 2026-05-07 | Daily pipeline schedule moved 15:30 → 15:15 ICT; freshness cutoff 15:30 → 15:00; persist RS matrices to GCS so manual re-renders use today's matrices | `cca1411` |
| 2026-05-07 | Intraday chart: render only the latest intraday tick (chart = 49 EOD + 1 live point) | `b29ab56` |
| 2026-05-06 | Breadth universe = `tickers.csv` top-100 (both intraday + EOD); fix EOD chart that was leaking 230 tickers | `1e5f49b` |
| 2026-05-06 | Intraday chart: 50-day EOD history + today's intraday ticks; force xaxis to category mode | `7d25194`, `3b88b95` |
| 2026-05-06 | Intraday chart: align JS line colors with MA_COLORS (mbz20 purple, mbz50 black bold) | `c69bebe`, `c6d3c33` |
| 2026-05-06 | Intraday breadth: SMA from EOD closes only (no self-reference) + add T-1 EOD anchor | `d745c06`, `f50fb5e` |
| 2026-05-06 | Add 15-min intraday breadth chart polling JSON on GCS; new Cloud Run job + Scheduler | `f7e6896` |
| 2026-05-06 | Crypto RS heatmap (top-50 vs BTC); drop in-progress UTC bar; surface update time + UTC close in ICT | `73f9f91`, `14b2dc3`, `8535c30` |
| 2026-05-06 | Suppress Universe Drift Alert banner (false positives from 58 manual additions to rs_fixed_tickers) | `dc5064a` |
| 2026-05-06 | Unify RS universe to `rs_fixed_tickers.csv` (172→230); pre-breakout gates on composite RS rating | `bd5363e` |
| 2026-05-06 | RS matrix composite blend tuned 50/50 → 30% RS + 70% momentum | `bd5363e` |

### April 2026

| Date | Change | Commit |
|---|---|---|
| 2026-04-29 | EOD downloader: drop dead SSI source (vnstock 3.5+ no longer supports it) | `8fbed93` |
| 2026-04-29 | Full pipeline deployed to Cloud Run; Dockerfile copies pipeline files; entrypoint runs `run_daily_update.py` | `16ede1c` |
| 2026-04-29 | `VNSTOCK_API_KEY` mounted via Secret Manager (lifts cloud rate limit from 20 → 60 req/min) | infra-only |
| 2026-04-29 | Cloud Scheduler corrected `0 8 * * 1-5` → `30 15 * * 1-5` ICT (post-15:00 close) | infra-only |
| 2026-04-29 | Pre-breakout panel (initial: Mansfield RS_Ratio + BB squeeze) | `16ede1c` |
| 2026-04-29 | VIX 50→100 sessions; Nasdaq 100-session chart added | `16ede1c` |

---

## 9. Known gaps and risks

1. **Cloud Run timeout edge.** Full daily pipeline now ~10–13 min on warm cache (was 5–8 min before crypto stage was added). Cold cache could push toward the 15-min limit. If we ever see timeouts, bump `--task-timeout=20m` on `market-breadth-job`.

2. **vnstock vendor risk.** Daily run hard-depends on KBS for VN EOD + VCI for intraday `Trading.price_board()`. If either source goes dark the downloader falls back through `["KBS", "VCI", "MSN", "FMP"]` — but the intraday job is hard-coded to VCI for `Trading.price_board()` (line in `intraday_breadth.py:fetch_current_prices`). Tested: VCI returns 100/100 in ~2s consistently.

3. **yfinance crypto coverage.** 10 of the 50 pinned coins fail to fetch (renamed/missing on Yahoo's feed). See [`docs/CRYPTO_RS_HEATMAP.md`](docs/CRYPTO_RS_HEATMAP.md) for the list and rename suggestions. Crypto matrix builds fine over the remaining ~39.

4. **No alerting on failed runs.** A failed Cloud Run execution does not page anyone. User notices via stale `Last-Modified`. Hooking up Cloud Run Job execution-failed → Slack / email is a 30-min add via Cloud Monitoring alert policy.

5. **GitHub Actions occasionally fails to fire** after a push. Workaround: `gcloud builds submit` directly. Observed once in May 2026 — has since fired reliably.

6. **`rs_universe.csv` is orphaned post-unification.** No code references it; it's just rotting in the repo. Safe to `git rm` in a follow-up.

7. **Universe Drift banner suppressed by design.** The 58 manual additions to `rs_fixed_tickers.csv` would otherwise trigger 58 false-positive "removals" daily. The drift script still runs and writes `logs/universe_drift_*.txt` for audit. To re-enable real drift detection, see [`docs/UNIVERSES.md`](docs/UNIVERSES.md) — needs a `lock_rule`-aware filter in `rs_universe_generator.py`.

8. **Manual HTML uploads can clobber the cloud-published HTML.** When uploading `market_breadth.html` locally, always pull `gs://vn-market-breadth/intraday/combined_dataset.csv` first to avoid publishing stale data over a fresher cloud-generated HTML. See [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

---

## 10. Backlog / natural next options

Ranked roughly by ROI for trading decisions.

1. **Email/Slack alert on Cloud Run failure** so a missed daily refresh is detected within minutes rather than at the next dashboard visit. Cloud Monitoring alert policy on `cloud_run_job` execution status.
2. **Move from `:latest` to immutable image tags** (`:<git_sha>`) so rollback is `gcloud run jobs update --image=...:<old_sha>` rather than guessing which image was good.
3. **Drift detector smart-filter**: skip rows in `rs_fixed_tickers.csv` where `lock_rule` starts with `"Manual addition"` so the drift report becomes useful again (currently suppressed at the dashboard layer because of 58 false positives).
4. **Update crypto universe** to cover the 10 yfinance-failed coins (rename `MATIC` → `POL`, `FTM` → `S`, `RNDR` → `RENDER`; drop or replace the rest).
5. **Add a sector heatmap** above RS heatmap — same data, aggregated by industry (already in `rs_matrix_3T.csv`).
6. **Add CBOE PCC ratio + US Treasuries 10y** below VIX/Nasdaq for the macro-flow view.
7. **Adaptive Bollinger period / RS lookback per-ticker** based on each name's typical vol — improves Layer B precision on volatile small-caps.
8. **Backtester harness** that replays a year of pre-breakout signals against forward returns — gives the user a concrete edge estimate.
9. **Delete `rs_universe.csv`** — orphaned post-unification.
10. **Migrate `.env` API-key handling to a single source-of-truth** (both `.env` and `~/.vnstock/api_key.json` exist locally).

---

## 11. How a future Claude session should bootstrap

1. **Read this file first.** Skim §1–4 for posture, §5 for cloud handles, §6 for verbs.
2. **Then read the topic docs** under [`docs/`](docs/) for whatever you're touching:
   - Editing the intraday chart → [`INTRADAY_BREADTH.md`](docs/INTRADAY_BREADTH.md).
   - Touching pre-breakout / RS rating → [`RS_AND_PREBREAKOUT.md`](docs/RS_AND_PREBREAKOUT.md).
   - Touching the crypto heatmap → [`CRYPTO_RS_HEATMAP.md`](docs/CRYPTO_RS_HEATMAP.md).
   - Anything universe-related → [`UNIVERSES.md`](docs/UNIVERSES.md).
   - Manual ops / debugging → [`OPERATIONS.md`](docs/OPERATIONS.md).
3. **Then read** `market_breadth.py` (the orchestrator + HTML template) and `intraday_breadth.py` / `pre_breakout.py`. These three files contain ~80% of the product logic.
4. **Check current state** with `git log -10`, `gcloud run jobs executions list --job=market-breadth-job --region=asia-southeast1 --limit=5`, and `curl -sI` against the public URL.
5. **For any change that touches the cloud**: edit locally → commit → push → wait ~2 min for Cloud Build → next scheduled run picks it up. Both Cloud Run jobs share the image; GHA refreshes both jobs' `:latest` digest after every build.

---

## 12. Contact / ownership

- **Product owner**: vtnhan@gmail.com (also the Git committer / GCP project owner / Cloud Scheduler email)
- **GCP project**: `project-feb6df0e-9749-4925-b4e`
- **GitHub**: vtnhan-bot/vn-market-breadth

End of KB.
