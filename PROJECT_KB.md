# VN Market Breadth Dashboard — Project Knowledge Base

> **Purpose**: a single document that lets a senior engineer (or a future Claude session) get from cold-start to confidently shipping changes in under 30 minutes. Read this first; it points you at everything else.
>
> **Last refresh**: 2026-04-29 (after the full-pipeline cloud deployment landed).

---

## 1. At a glance

| Item | Value |
|---|---|
| One-liner | Daily Vietnam-equity dashboard with US macro references (VIX, Nasdaq) and pre-breakout signal scanner. |
| Live URL | https://storage.googleapis.com/vn-market-breadth/index.html |
| Refresh cadence | Weekdays 15:30 Asia/Ho_Chi_Minh (08:30 UTC), automated end-to-end |
| Repo | https://github.com/vtnhan-bot/vn-market-breadth (master) |
| Local working dir | `D:\Claude\Market on website` (Windows) |
| GCP project | `project-feb6df0e-9749-4925-b4e` (region `asia-southeast1`) |
| GCS bucket | `vn-market-breadth` |
| Cloud Run job | `market-breadth-job` |
| Cloud Scheduler | `market-breadth-schedule` (cron `30 15 * * 1-5`, TZ Asia/Ho_Chi_Minh) |
| Primary user | CTO / portfolio manager — visits the URL daily, makes trade decisions from it |

---

## 2. What the dashboard contains (top to bottom)

The page (`market_breadth.html`) is built by `market_breadth.py` and uploaded to GCS as `index.html`.

1. **Breadth chart** — % of top-100 HOSE+HNX stocks above SMA-3/5/10/20/50/200 over the last 50 sessions. Six lines on one plot. The headline indicator.
2. **VNINDEX 50-session candlestick** with volume.
3. **CBOE VIX — 100 phiên** candlestick (yfinance `^VIX`).
4. **Nasdaq Composite — 100 phiên** candlestick (yfinance `^IXIC`).
5. **Breadth detail tables** — current SMA-N readings + day/week deltas, and a composite gauge with a Vietnamese-language verdict ("THẬN TRỌNG" / "TÍCH CỰC" / etc.).
6. **🚀 Pre-breakout panel** *(new — Apr 2026)* — two-layer scanner over the RS monitor universe:
   - **Layer A (RS Line Divergence)**: stocks whose RS Line (`close / vnindex_close`) is at a 252-day high while price is still ≤ 95% of its 252-day high → relative strength is leading, price is still in a base.
   - **Layer B (RS_Ratio + BB Squeeze)**: stocks where Mansfield ratio `(1 + stock_6mo_return) / (1 + vni_6mo_return) > 1.20` **and** Bollinger Band(20, 2σ) width is in the bottom 20% of its trailing 126-session distribution.
   - Each layer has a "🔥 Triggered" list (strict criteria) and a "👀 Watch" list (10 closest-to-trigger candidates).
   - Highlighted "⭐ Both" block at the top when a ticker passes both layers.
7. **RS Heatmap** — 90-day Mansfield-style RS percentile vs. VNINDEX, last 19 sessions × 170 institutional tickers, color-coded leader/strong/neutral/laggard.

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

Four sequential stages. Each is a standalone script that can be run individually for local debugging.

| # | Script | What it does | Reads | Writes |
|---|---|---|---|---|
| 1 | `eod_batch_downloader.py` | Fetches today's EOD bar for top-100 tickers via vnstock (KBS source) with rate-limit pacing. | `tickers.csv`, `cache/*.pkl` | `data/<today>/*.csv`, `data/<today>/combined_dataset.csv` |
| 2 | `rs_universe_generator.py` | Refreshes the 200-ticker RS universe from `institutional_universe_3T.csv` plus drift detection. | `institutional_universe_3T.csv`, `rs_fixed_tickers.csv`, `cache/rs_history/*.csv` | `rs_universe.csv`, `logs/universe_drift_latest.txt` |
| 3 | `rs_matrix_3T.py` | Computes 90-day Mansfield RS percentiles for the 170-ticker fixed universe over the last 19 sessions. | `rs_fixed_tickers.csv`, `cache/rs_history/*.csv` | `rs_matrix_3T.csv` |
| 4 | `market_breadth.py` | Computes mbz3/5/10/20/50/200, fetches yfinance VIX + Nasdaq, builds HTML with all panels, then calls `_patch_pre_breakout` to inject Layer A/B. | `data/<today>/combined_dataset.csv`, `rs_matrix_3T.csv`, `rs_universe.csv` | `market_breadth.html` |

A successful run produces a single self-contained `market_breadth.html` (~2 MB, all data inlined as JSON in `<script>` blocks).

### Freshness gate
`market_breadth.py` will **abort with `"CRITICAL: EOD Data Not Fresh"`** if `combined_dataset.csv` was modified before **15:30 ICT today**. This is intentional — guarantees no run ships yesterday's prices as today's. The scheduler is therefore set to fire at 15:30 ICT exactly so the downloader writes fresh data right before the gate check.

### Pre-breakout integration
At the very end of `market_breadth.py main()`, after `build_html()` writes the HTML, the script imports `_patch_pre_breakout.build_panel` and surgically injects the pre-breakout panel HTML + CSS into the file. Wrapped in `try/except` so a missing `pre_breakout` module never breaks the breadth pipeline.

---

## 5. Cloud infrastructure reference

### 5.1 Cloud Run Job

```
Name:      market-breadth-job
Region:    asia-southeast1
Image:     asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth:latest
Resources: cpu=2, memory=2Gi, task-timeout=15m, max-retries=1
Service account: 746287134716-compute@developer.gserviceaccount.com
Env vars:
  - GITHUB_ACTIONS=true     (suppresses browser open + Excel ticker source)
  - VNSTOCK_API_KEY         (mounted from Secret Manager: vnstock-api-key:latest)
```

### 5.2 Cloud Scheduler

```
Name:           market-breadth-schedule
Cron:           30 15 * * 1-5
Timezone:       Asia/Ho_Chi_Minh
Target:         POST  https://asia-southeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/.../jobs/market-breadth-job:run
Auth:           OAuth (service account: github-deploy@…iam.gserviceaccount.com)
Attempt deadline: 180s   (just to start the job — the job's own timeout is 15 min)
```

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
├── index.html              ← public dashboard (Cache-Control: no-cache, no-store, must-revalidate)
└── cache/                  ← incremental-fetch cache, restored at job start, persisted at job end
    ├── *.pkl               ← per-ticker breadth pickles
    ├── rs_history/*.csv    ← per-ticker history for RS universe (~234 tickers)
    └── rs_company_overview_cache.csv
```

### 5.5 GitHub Actions

```
Workflow:  .github/workflows/update_chart.yml
Triggers:  push to master, manual dispatch
Steps:
  1. Auth to GCP via Workload Identity Federation
  2. gcloud builds submit  →  pushes :latest to Artifact Registry
  3. gcloud run jobs update --image …:latest
```

The workflow occasionally reports false-failure on the `gcloud builds submit` step due to async log streaming — verify by checking `gcloud builds list --region=asia-southeast1`. If the build itself shows `SUCCESS`, the deployment is fine.

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

| Setting | Value | Where | Why |
|---|---|---|---|
| `SESSIONS_SHOW` (breadth window) | 50 | `market_breadth.py` | Headline chart length |
| `US_INDEX_SESSIONS` | 100 | `market_breadth.py` | VIX & Nasdaq lookback |
| `MA_PERIODS` | `[3, 5, 10, 20, 50, 200]` | `market_breadth.py` | mbz lines on the breadth chart |
| `freshness_cutoff` | 15:30 ICT | `market_breadth.py:96` | Aborts run if data older than this |
| `API_CALL_DELAY_SECONDS` | 1.1 | `eod_batch_downloader.py` | Stays under 60 req/min (community tier) |
| `API_SOURCES` | `["KBS", "VCI", "MSN", "FMP"]` | `eod_batch_downloader.py` | vnstock 3.5+ supported sources (SSI/VND removed) |
| `WINDOW_52W` | 252 | `pre_breakout.py` | Layer A rolling-max window |
| `RS_HIGH_TOL` | 0.99 | `pre_breakout.py` | "RS at high" if ≥ 99% of 252-d max |
| `PRICE_BASE_MAX` | 0.95 | `pre_breakout.py` | "In a base" if price ≤ 95% of 252-d high |
| `RETURN_LOOKBACK` | 126 | `pre_breakout.py` | ~6 months for Mansfield ratio |
| `RS_RATIO_THRESH` | 1.20 | `pre_breakout.py` | Layer B trigger threshold |
| `BB_PERIOD`, `BB_K` | 20, 2.0 | `pre_breakout.py` | Bollinger Bands |
| `SQUEEZE_PCTILE` | 20.0 | `pre_breakout.py` | Bottom 20% of trailing BB widths |
| Cloud Run `task-timeout` | 900s (15 min) | gcloud config | Buffer for full pipeline + cache cold-start |
| Cloud Run `memory` | 2Gi | gcloud config | pandas + RS calcs comfortably fit |

---

## 8. Recent changelog (Apr 2026)

| Date | Change | Commit |
|---|---|---|
| 2026-04-29 | EOD downloader: drop dead SSI source (vnstock 3.5+ no longer supports it) | `8fbed93` |
| 2026-04-29 | Full pipeline deployed to Cloud Run; Dockerfile copies all 9 pipeline files; entrypoint runs `run_daily_update.py` | `16ede1c` |
| 2026-04-29 | `VNSTOCK_API_KEY` mounted via Secret Manager (lifts cloud rate limit from 20 → 60 req/min) | infra-only |
| 2026-04-29 | Cloud Scheduler corrected from `0 8 * * 1-5 (ICT)` (08:00 ICT, broken by freshness gate) → `30 15 * * 1-5 (ICT)` | infra-only |
| 2026-04-29 | Pre-breakout panel: Layer A (RS Line divergence) + Layer B (Mansfield RS_Ratio + BB squeeze) over RS universe | `16ede1c` |
| 2026-04-29 | VIX chart extended 50 → 100 sessions | `16ede1c` |
| 2026-04-29 | Nasdaq Composite 100-session chart added below VIX | `16ede1c` |
| Earlier | Switch from TCBS/VCI to KBS as primary vnstock source | `91ec0e9` |
| Earlier | Move from gsutil → Python GCS upload in entrypoint | `66188ba` |

---

## 9. Known gaps and risks

1. **Pre-breakout coverage gap (94 / 200 RS-universe tickers).** The EOD downloader fetches the top-100 universe (`tickers.csv`), but the RS monitor list is 200 tickers. Layer A/B currently analyse only the 94 that overlap. To close this gap: extend `eod_batch_downloader.py` to also fetch the 106 RS-only tickers (or add a second pass that pulls just those into `data/<today>/combined_dataset.csv`).

2. **Cloud Run timeout edge.** The full pipeline is ~5–8 min on a warm cache, but a cold cache (no `gs://…/cache/rs_history/`) can push universe generation to 10+ min. Combined with downloader + matrix + breadth, total can creep toward the 15-min limit. If this becomes routine: bump `--task-timeout=20m` or split universe gen into a weekly job that doesn't run daily.

3. **vnstock vendor risk.** Daily run hard-depends on KBS via vnstock. If KBS goes dark, the downloader falls back through `["VCI", "MSN", "FMP"]` — but those need to be tested. There's no synthetic-data fallback for shipping a clearly-marked stale dashboard.

4. **No alerting.** A failed Cloud Run execution does not page anyone. The user notices when they visit the URL and see a stale `Last-Modified`. Hooking up Cloud Run Job execution-failed → Slack / email is a 30-min add via Cloud Monitoring alert policy.

5. **GitHub Actions reports false failures** on `gcloud builds submit` when log streaming hiccups. Always verify with `gcloud builds list` directly before debugging.

6. **`market_breadth.html` has Windows / mojibake risk** in some hardcoded labels (was `phiÃªn` instead of `phiên`). Fixed in current code; watch for regressions when adding new strings.

7. **Universe drift unmonitored.** `rs_universe_generator.py` writes drift logs but no alert if the institutional universe changes meaningfully week-over-week. A drift > 3 tickers shows a banner on the dashboard via `SIGNIFICANT_DRIFT_THRESHOLD`, but nothing pages out.

---

## 10. Backlog / natural next options

Ranked roughly by ROI for trading decisions.

1. **Backfill the 106 missing RS tickers** in the EOD downloader so Layer A/B cover the full 200-ticker monitor universe (the panel becomes 2× as useful).
2. **Email/Slack alert on Cloud Run failure** so a missed daily refresh is detected within minutes rather than at the next dashboard visit.
3. **Add intraday refresh** (e.g. 13:00 ICT mid-day snapshot) — would require a second Cloud Scheduler job and either a separate Cloud Run Job or a flag-aware single job.
4. **Move from `:latest` to immutable image tags** (`:<git_sha>`) so rollback is `gcloud run jobs update --image=...:<old_sha>` rather than guessing which image was good.
5. **Add a sector heatmap** above RS heatmap — same data, aggregated by industry (already in `rs_matrix_3T.csv`).
6. **Add CBOE PCC ratio + US Treasuries 10y** below VIX/Nasdaq for the macro-flow view.
7. **Adaptive Bollinger period / RS lookback per-ticker** based on each name's typical vol / consolidation length — improves Layer B precision on volatile small-caps.
8. **Backtester harness** that replays a year of pre-breakout signals against forward returns — gives the user a concrete edge estimate.
9. **Migrate `.env` API-key handling to a single source-of-truth** (currently both `.env` and `~/.vnstock/api_key.json` exist locally) to reduce config drift.
10. **Multi-region GCS for resilience** if the user starts depending on this for live decisions.

---

## 11. How a future Claude session should bootstrap

1. **Read this file first.** Skim §1–4 for posture, §5 for cloud handles, §6 for verbs.
2. **Then read** `market_breadth.py` (the orchestrator + HTML template) and `pre_breakout.py` (the signal engine). These two files contain ~80% of the product logic.
3. **Check current state** with `git log -10`, `gcloud run jobs executions list --job=market-breadth-job --region=asia-southeast1 --limit=5`, and a `curl -sI` against the public URL.
4. **For any change that touches the cloud**: change locally → commit → push → wait 2-3 min for Docker rebuild → next scheduled run picks it up. No manual gcloud deploy needed unless changing job config (timeout/memory/secrets/image-pin).

---

## 12. Contact / ownership

- **Product owner**: vtnhan@gmail.com (also the Git committer / GCP project owner / Cloud Scheduler email)
- **GCP project**: `project-feb6df0e-9749-4925-b4e`
- **GitHub**: vtnhan-bot/vn-market-breadth

End of KB.
