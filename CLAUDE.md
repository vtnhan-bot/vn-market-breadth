# Market on Website (VN Market Dashboard) — Claude Code Context

## What This Is

Interactive Vietnamese stock market dashboard with real-time breadth analysis, RS heatmaps, and a pre-breakout scanner. Targets top-100 HOSE+HNX stocks.

**Dashboard output:** `gs://vn-market-breadth/` (public GCS bucket)

**What it shows:**
- EOD market breadth charts (% of stocks above 6 moving averages: mbz3/5/10/20/50/200)
- Live intraday breadth updates (every 15 min during VN market hours)
- VNINDEX candlesticks + VNINDEX ex-VIC/VHM/VRE
- US macro panel: VIX, Nasdaq, DXY
- Pre-breakout scanner (RS Rating ≥90 + Bollinger Band squeeze)
- RS heatmaps for ~230 VN stocks + top-50 crypto vs BTC

**Tech stack:** Python 3.11, vnstock, yfinance, pandas, Vanilla JS/HTML/Chart.js, Docker

## Run Commands

```bash
# Full daily pipeline (EOD update):
python run_daily_update.py

# Intraday breadth update (every 15 min during market hours):
python intraday_breadth.py

# Entry point (Cloud Run):
bash entrypoint.sh
```

**Session windows (ICT):** Morning 09:00–11:30, Afternoon 13:00–14:45

## Key Files

| File | Purpose |
|---|---|
| `market_breadth.py` | Main HTML generator |
| `run_daily_update.py` | Pipeline orchestrator |
| `intraday_breadth.py` | Live breadth updates |
| `rs_matrix_3T.py` | RS heatmap builder |
| `intraday_rs_3T.py` | Intraday RS heatmap |
| `entrypoint.sh` | Cloud Run entry point |
| `Dockerfile` | Container (already exists) |
| `PROJECT_KB.md` | Full technical docs |
| `infra/` | Billing killswitch + Telegram CF |
| `.github/workflows/update_chart.yml` | CI/CD (GitHub Actions) |

---

## GCP Deployment & Cost Safety

> Canonical target: project **`project-feb6df0e-9749-4925-b4e`** (account vtnhan@gmail.com),
> regions **us-central1** (free e2-micro VM `pattern-engine`) + **asia-southeast1** (Cloud Run).
> **Free-tier-only.** Full fleet architecture: `d:\Claude\Devops\ARCHITECTURE.md`.

### This engine on GCP  (corrected 2026-07-17 — verified against live infra)
- **Runs as:** systemd oneshots **on the e2-micro VM `pattern-engine`** (us-central1-a), user `marketbreadth`,
  `/opt/market-breadth`. Timers: `market-breadth.timer` (Mon-Fri 15:15 + Tue-Sat 07:30 ICT) → `run.sh`;
  `intraday-breadth.timer` (*/15 09-14 ICT) → `run_intraday.sh`. Migrated off Cloud Run 2026-06-27.
- **The Cloud Run jobs `market-breadth-job` / `intraday-breadth-job` are DELETED, not dormant.** There is **no
  Cloud Run fallback and no rollback target**. `gcloud run jobs list` (all regions) returns only
  `us-market-breadth-job`, a different engine. The root `entrypoint.sh` / `deploy.sh` / `Dockerfile` are dead
  Cloud Run paths that nothing invokes.
- **Note:** `rs_matrix_crypto.py` here is the shared crypto RS engine that Cointrading also imports; it uses
  **KuCoin** (not Binance) — keep it KuCoin.
- **Sync / deploy:** `/opt/market-breadth` is **not a git checkout and there is no CI** — pushing to master
  deploys nothing. Deploy by hand: `gcloud compute scp <files> pattern-engine:/tmp/` then
  `sudo install -o marketbreadth -g marketbreadth -m 0664 /tmp/<f> /opt/market-breadth/<f>`; verify with
  `md5sum` + `venv/bin/python3 -m py_compile`. **Back up what you replace first.** Version-controlled copies of
  the VM's own artifacts (run wrappers, systemd units, setup scripts) live in [`deploy/vm/`](deploy/vm/).
- **Before touching the EOD cache or `verify_fresh_eod_dataset`:** read [`docs/STALE_DASHBOARD_FIX.md`](docs/STALE_DASHBOARD_FIX.md).
  A persistent-local-cache + mtime-only guard silently republished T‑1 every Tue-Fri for ~2.5 weeks.

### 💸 Cost reality (verified 2026-07-17 — supersedes the "exactly $0" framing below)
- **The project is NOT at ₫0, and there is NO 2026-07-18 "cliff".** The account **already converted Free Trial →
  paid ~2026-07-01** (proof: Always Free only applies to paid accounts, and Regional Class A free ops = **0 in
  June** vs **exactly 5,000 in July**). On 07-18 nothing is suspended or deleted — the leftover promo just runs
  out and ≈**60,000 VND/mo (~$2.30)** starts hitting the card. The e2-micro survives regardless (its core+RAM are
  zeroed by negative-cost free-tier SKUs, not by the promo). **Don't act on urgency from older notes.**
- **"Stay under GCS 5 GB" below is WRONG for this engine.** Always Free *storage* is US-regions-only, so
  `gs://vn-market-breadth` (asia-southeast1) gets 0 free bytes at any size — but Always Free *operations* are a
  **shared cross-region pool** that Singapore already draws from pro-rata, and it is **100% exhausted** each month
  by Cointrading's us-central1 buckets. Moving the bucket to us-central1 saves **5.46 VND/mo** and costs a
  measured **2.53×** VN latency regression → **NO-GO**.
- **Billing-export trap:** the GCS free tier is a **zero-rated price tier** (`price.tier_start_amount = 0` →
  `effective_price = 0` → `cost = 0.0`), **not** a `FREE_TIER` credit row and **not** a negative-cost SKU (that's
  Compute Engine only). Always split by `price.tier_start_amount` via `UNNEST([price]) p` — a `cost + credits`
  query reports "zero free tier" and is wrong.
- Full detail + the arithmetic: [`docs/STALE_DASHBOARD_FIX.md`](docs/STALE_DASHBOARD_FIX.md) §5.

### 🚫 Cost guardrails — never incur charges
- **Never create a Cloud Scheduler job.** Free tier = 3 jobs total; the fleet sits at **0** (verified 2026-07-17).
  **`gcp-stop-jul13` does NOT exist** in any of the 27 scheduler locations — the documented "auto-unlink billing
  on 2026-07-13" backstop is **not armed**, and its absence went unnoticed. The count is fine; the *safety* is gone.
  To schedule a Cloud Run job, add a **VM systemd timer** instead — pattern in `d:\Claude\Devops\gcp\infra\`
  (`engine-*.timer` + `engine-job@.service`; the VM SA `pattern-engine-sa` has custom role
  `cronJobRunner` to execute jobs). Verify next fires with `systemctl list-timers 'engine-*'`.
- **Never create a 2nd VM**, a non-`e2-micro` instance, or a VM outside us-central1/us-west1/us-east1.
- **Never add** Cloud SQL, Memorystore, GKE, load balancers, or reserved static IPs.
- Stay under free limits: Cloud Run 2M req/mo · GCS 5 GB · Artifact Registry 0.5 GB · 3 Scheduler jobs.
- **Crypto market data MUST use KuCoin**, never Binance (Binance returns HTTP 451 from us-central1).
- The Devops billing monitor + 95% auto-detach killswitch backstop catastrophe, but won't catch
  small sub-dollar drift — the design goal is **exactly $0**.
