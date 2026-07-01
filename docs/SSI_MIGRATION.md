# vnstock → SSI FastConnect Migration & Session Handoff (2026-06-22)

Durable, version-controlled record of the SSI data-source migration and the current operational
status of the breadth dashboard. Read this alongside [`../PROJECT_KB.md`](../PROJECT_KB.md). For the
AI-side restart context see the memory files under
`C:\Users\DELL\.claude\projects\d--Claude-Market-on-website\memory\` (start with `project_current_status.md`).

---

## 1. Why this happened

On 2026-06-22 (~09:30 ICT) vnstock's live **`Trading.price_board()`** began returning **HTTP 403** from
Cloud Run (an external VN-provider block). That froze the intraday breadth chart and the intraday RS
heatmap. vnstock's `quote.history` (used by the EOD downloader) was still working but degrading. SSI
FastConnect is the proven, GCP-reachable feed already powering the `trading-signal` engine on the VM, so
the dashboard's market data was migrated onto it.

## 2. What changed (all on branch `infra-cost-protection`, PR #1)

| File | Change |
|---|---|
| `ssi_client.py` **(new)** | Self-contained SSI FastConnect Data client, adapted from VN trading-signal's `feed/api_client.py`. `get_current_prices(tickers) → {TICKER_UPPER: price/1000}` (one `intraday_ohlc` call/ticker, token-bucket rate-limited ~0.9 req/s, 429-retry, bad symbols skipped). `get_daily_ohlcv(symbol, start, end)` → canonical OHLCV (raw VND). Lazy SDK import; creds from `SSI_FC_DATA_CONSUMER_ID/_SECRET` or `ID/secret`. |
| `intraday_breadth.py` | `fetch_current_prices()` → `ssi_client.get_current_prices()`. |
| `intraday_rs_3T.py` | `_fetch_intraday_prices()` → SSI. `price_board`'s `ref_price` replaced by the prior-session EOD close from `combined_dataset` (`_ref_close_from_history`, `time < today`). Dropped dead `PRICE_DIVISOR`. |
| `eod_batch_downloader.py` | `fetch_with_failover()` now tries **SSI primary** (`_fetch_ssi_daily`, OHLC ÷1000), **vnstock fallback** (KBS/VCI/MSN/FMP) for any symbol SSI can't serve. Added source/scale probe logging. |
| `rs_source2.py` | OHLC fetch → SSI-primary/vnstock-fallback. `Listing` + `Company.overview` stay on vnstock (SSI has no listing/fundamentals endpoint; these are not on the deployed pipeline). |
| `requirements.txt`, `Dockerfile` | `+ ssi-fc-data==2.2.2`; `COPY ssi_client.py`. |
| `docs/INTRADAY_RS.md`, `docs/INTRADAY_BREADTH.md` | Data-source + timeout (120→720s) updates. |

### Critical contract facts (verified, do not break)
- `combined_dataset.csv` `close` is **thousand-VND** (FPT 70.6, VNINDEX 1.86). SSI returns **raw VND** →
  **÷1000** everywhere. The intraday tick compares against SMAs from this file, so the scale MUST match.
- SSI **does** serve VNINDEX via `daily_ohlc` (1.8579 = 1860 pts/1000) — no special index handling needed.
- Keys are UPPER-cased on both sides.

## 3. Verification (in-cloud smoke tests — subagent reviewers were session-capped)

| Swap | Evidence |
|---|---|
| Intraday breadth | live job: `Got prices for 97–99/100`, `intraday_breadth.json` published |
| Intraday RS | standalone dry-run: `RS history 230` → `SSI current prices 229/230` → `would publish 229 rows` |
| EOD downloader | standalone: `Source coverage {'SSI': 230, 'KBS': 1}` (NT2 fell back), `Valid 231 / Failed 0` |
| **Full EOD pipeline** | `market-breadth-job` exited 0; `combined_dataset {'SSI': 64395}` (every row), `index.html` rebuilt |

Timing: intraday breadth sweep ~122s (100 tickers); RS sweep ~265s (230); EOD ~268s (231). All within
the 720s intraday / 900s EOD task-timeouts.

## 4. Deploy state (already applied via manual `gcloud builds submit`)

- **Image:** `asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth:latest`
  (~`sha256:5b0c8659…`).
- **`intraday-breadth-job`** — new image, `--task-timeout=720s`, SSI secrets injected.
- **`market-breadth-job`** — new image, SSI secrets injected (900s timeout unchanged).
- **Secret Manager:** `SSI_FC_DATA_CONSUMER_ID`, `SSI_FC_DATA_CONSUMER_SECRET` (granted to
  `746287134716-compute@…`, the SA both jobs run as).
- Both jobs are triggered by **VM systemd timers** on `pattern-engine` (NOT Cloud Scheduler):
  `engine-intraday-breadth.timer` (*/15 9-14 ICT), `engine-market-breadth.timer` (15:15 + 07:30 ICT).
  First fully-SSI scheduled runs: **07:30 ICT (EOD) + 09:00 ICT (intraday)** the next trading day.

## 5. What still uses vnstock (and why it's fine)

Only `rs_source2.py`'s `Listing` (universe discovery) and `Company.overview` (fundamentals:
outstanding_shares). SSI is a market-data feed with **no listing/fundamentals endpoint**, so these
structurally cannot move — and they are **dead code in the deployed pipeline** (`rs_matrix_3T` /
`rs_universe_generator` import only constants + `configure_logging` from that module; the live RS step
reads OHLC from the SSI-sourced `combined_dataset.csv`). Only the non-deployed `rs_matrix_builder.py`
calls them.

## 6. DevOps context (where things run)

- **VM `pattern-engine`** (e2-micro, us-central1-a) = job scheduling (systemd timers) + always-on
  engines (`pattern-engine`, `trading-signal`, `cointrading-alerter`, `cointrading-tb-collector`).
  **Single point of failure** — if it's down, nothing fires.
- **Cost-protection** = `gcp-billing-monitor` (Gen2 Cloud Function, Pub/Sub budget-triggered) — serverless,
  off-VM by design (a VM-hosted killswitch couldn't stop a runaway VM).
- **Free-tier-only.** Cloud Scheduler holds 1 job (`gcp-stop-jul13`). Never add a 2nd VM or scheduler job.

## 7. Broader status / open threads

- **Credit cliff:** breadth project runs on FREE-TRIAL credit (₫7.18M) **expiring 2026-07-18**. Net ₫0 now,
  but dies then. Armed backstops: `gcp-stop-jul13` (auto-unlink billing 2026-07-13 03:00 ICT) + a July-10
  migration reminder.
- **Pending GCP-exit migration:** OCI Always-Free capacity hunt (24/7 grabber, all shapes "out of host
  capacity" in Singapore; PAYG upgrade in progress) → deploy Trader Lion to OCI + retire the Cloud Run
  Jobs bridge → move dashboards to Cloudflare R2/Pages → let GCP lapse.

## 8. Commits

```
32e1852  rs_source2: SSI-primary OHLC; tidy downloader log wording
4085f1f  EOD downloader: SSI FastConnect primary, vnstock fallback
245837f  Docs: intraday RS now on SSI; intraday job timeout 720s
489bed6  Intraday RS: replace vnstock price_board with SSI FastConnect
2163d8f  Intraday breadth: replace vnstock price_board with SSI FastConnect
```

Branch `infra-cost-protection` pushed to `origin`; **PR #1** → `master`
(https://github.com/vtnhan-bot/vn-market-breadth/pull/1). Merging triggers `update_chart.yml` to rebuild
from `master` — consistent with the already-deployed images.
