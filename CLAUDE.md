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

### This engine on GCP
- **Runs as:** Cloud Run jobs **`market-breadth-job`** + **`intraday-breadth-job`** (asia-southeast1, `market-repo`
  image), triggered by VM systemd timers `engine-market-breadth.timer` (15:15 ICT + 07:30 ICT Tue-Sat) and
  `engine-intraday-breadth.timer` (*/15 9-14 ICT) — NOT Cloud Scheduler.
- **Note:** `rs_matrix_crypto.py` here is the shared crypto RS engine that Cointrading also imports; it uses
  **KuCoin** (not Binance) — keep it KuCoin.
- **Sync / deploy:** build + push the `market-repo` image, then the existing Cloud Run jobs pick it up. To change
  cadence, edit the VM timers, not Cloud Scheduler.

### 🚫 Cost guardrails — never incur charges
- **Never create a Cloud Scheduler job.** Free tier = 3 jobs total; the fleet sits at **1** (`gcp-stop-jul13`) deliberately.
  To schedule a Cloud Run job, add a **VM systemd timer** instead — pattern in `d:\Claude\Devops\gcp\infra\`
  (`engine-*.timer` + `engine-job@.service`; the VM SA `pattern-engine-sa` has custom role
  `cronJobRunner` to execute jobs). Verify next fires with `systemctl list-timers 'engine-*'`.
- **Never create a 2nd VM**, a non-`e2-micro` instance, or a VM outside us-central1/us-west1/us-east1.
- **Never add** Cloud SQL, Memorystore, GKE, load balancers, or reserved static IPs.
- Stay under free limits: Cloud Run 2M req/mo · GCS 5 GB · Artifact Registry 0.5 GB · 3 Scheduler jobs.
- **Crypto market data MUST use KuCoin**, never Binance (Binance returns HTTP 451 from us-central1).
- The Devops billing monitor + 95% auto-detach killswitch backstop catastrophe, but won't catch
  small sub-dollar drift — the design goal is **exactly $0**.
