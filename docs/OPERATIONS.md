# Operations Playbook

> **⚠ GCP note (updated 2026-06-21):** The schedule table below presents Cloud Scheduler jobs (`market-breadth-schedule`, `intraday-breadth-schedule`, `market-breadth-us-close`) and "crypto via Binance" as live behavior — this is stale. Current reality: the `market-breadth-job` and `intraday-breadth-job` Cloud Run jobs are triggered by **VM systemd timers** (`engine-market-breadth.timer` / `engine-intraday-breadth.timer` on the pattern-engine VM), **NOT Cloud Scheduler**. Crypto market data uses **KuCoin**, NOT Binance.
> Canonical current state: this project's CLAUDE.md → "GCP Deployment & Cost Safety", and d:\Claude\Devops\ARCHITECTURE.md. Content below is kept for reference.

Common operational situations and how to handle them. All times in **Asia/Ho_Chi_Minh (ICT, UTC+7)**.

## Schedules currently running

| Schedule | Cron (ICT) | Triggers | Avg duration | Purpose |
|---|---|---|---|---|
| `market-breadth-schedule` | `15 15 * * 1-5` | `market-breadth-job` | ~13–15 min (finishes ~15:28) | Daily post-VN-close pipeline. Hits the strict freshness branch (`verify_fresh_eod_dataset`): today's `data/<today>/combined_dataset.csv` must exist and be modified after 15:00 ICT. Builds matrices, regens HTML, uploads, persists `combined_dataset.csv` + `rs_matrix_3T.csv` + `rs_matrix_crypto.csv` to `gs://vn-market-breadth/intraday/`. End of run also rewrites `intraday_breadth.json` to a `Đóng cửa` rightmost-tick state. |
| `intraday-breadth-schedule` | `*/15 9-14 * * 1-5` | `intraday-breadth-job` | ~10–30s | Every 15 min during VN trading hours; the script's own check no-ops outside 09:30–11:30 / 13:00–14:45. First tick after a new day's 09:00 cron rolls `intraday_breadth.json` forward (incorporates yesterday's close into `eod_history`). |
| `market-breadth-us-close` *(added May 2026)* | `30 7 * * 2-6` | `market-breadth-job` | ~9–10 min | Post-US-close + post-UTC-crypto-close refresh. 07:30 ICT is 30 min after the 07:00 ICT crypto UTC daily-bar close and 3–4h after US market close. Hits the permissive freshness branch (`now < 15:00 ICT` → fall back to most-recent `data/<DATE>/` folder). Re-pulls VIX / Nasdaq via yfinance (skipping any in-progress US daily bar) and crypto via Binance, then republishes HTML. |

## Verbs you'll actually use

### Trigger the daily pipeline manually (any time of day)

```bash
gcloud run jobs execute market-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e \
  --region=asia-southeast1 \
  --wait
```

`verify_fresh_eod_dataset()` branches on the current ICT time:

- **`now ≥ 15:00 ICT`** → strict path. Requires today's `data/<today>/combined_dataset.csv` to exist and be modified after 15:00 ICT (matches the production 15:15 ICT cron). If you trigger after 15:00 ICT but today's EOD download hasn't run, the pipeline aborts with `CRITICAL: EOD Data Not Fresh`.
- **`now < 15:00 ICT`** → permissive path. Walks `DATA_DIR.glob("*/combined_dataset.csv")` and picks the most recent dated folder. Logs `Pre-15:00 ICT run; using most-recent EOD dataset <folder>`. This is what the 07:30 ICT `market-breadth-us-close` cron and ad-hoc morning triggers hit. Breadth chart reflects the most recent available close (often yesterday), while VIX/Nasdaq/crypto get re-fetched fresh.

### Trigger an intraday tick manually (any time)

```bash
gcloud run jobs execute intraday-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e \
  --region=asia-southeast1 \
  --update-env-vars=INTRADAY_FORCE=1 \
  --wait

# After verifying, REMOVE the env var so future scheduled fires
# respect the trading-window check:
gcloud run jobs update intraday-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e \
  --region=asia-southeast1 \
  --remove-env-vars=INTRADAY_FORCE
```

### Tail latest execution logs

```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="market-breadth-job"' \
  --project=project-feb6df0e-9749-4925-b4e --limit=200 --order=desc \
  --format="value(textPayload)" | head -200
```

Replace `market-breadth-job` with `intraday-breadth-job` for intraday.

### Rebuild the dashboard HTML locally and push

When the cloud's HTML is stale or wrong and you want it fixed before tomorrow's 15:30 ICT pipeline:

```bash
# 1. Pull all three freshest CSVs that the cloud persisted (combined + matrices).
mkdir -p data/$(date +%Y-%m-%d)
gsutil cp gs://vn-market-breadth/intraday/combined_dataset.csv \
  data/$(date +%Y-%m-%d)/combined_dataset.csv
gsutil cp gs://vn-market-breadth/intraday/rs_matrix_3T.csv .
gsutil cp gs://vn-market-breadth/intraday/rs_matrix_crypto.csv .

# 2. Regenerate locally (uses your working tree's market_breadth.py).
.venv/Scripts/python.exe market_breadth.py --no-browser

# 3. Upload to GCS with no-cache headers.
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
  cp market_breadth.html gs://vn-market-breadth/index.html

# 4. Verify.
curl -s -I https://storage.googleapis.com/vn-market-breadth/index.html | grep -i last-modified
```

⚠️ **Don't skip step 1.** If you regenerate from stale local CSVs, you'll publish an HTML that's older than what the cloud already produced. The cloud's three CSVs at `gs://vn-market-breadth/intraday/` are the source of truth — combined_dataset (EOD prices), rs_matrix_3T (VN RS Rating), rs_matrix_crypto (crypto RS Rating). All three are persisted at the end of every daily pipeline run.

### Re-pin a Cloud Run job to the freshest `:latest` digest

Cloud Run resolves the `:latest` tag to a digest at job-update time. After a new build, the `:latest` symlink in Artifact Registry updates, but the Cloud Run job still points at the old digest until you `gcloud run jobs update`. The GHA workflow does this automatically; if it fails, force it manually:

```bash
LATEST=$(gcloud artifacts docker tags list \
  asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth \
  --filter="tag.basename()=latest" --format="value(version)")

gcloud run jobs update intraday-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e --region=asia-southeast1 \
  --image=asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth@$LATEST
```

Then switch back to `:latest` after smoke-testing so future pushes auto-flow:
```bash
gcloud run jobs update intraday-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e --region=asia-southeast1 \
  --image=asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth:latest
```

### Reset `intraday_breadth.json` (e.g. before re-running with a fix)

```bash
echo '{"date":"YYYY-MM-DD","eod_history":[],"updates":[],"last_updated_ict":null}' \
  | gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
           -h "Content-Type:application/json" \
           cp - gs://vn-market-breadth/intraday_breadth.json
```

Use today's date in `YYYY-MM-DD`. After the next intraday-job execution, the JSON is repopulated correctly.

## Diagnostics

### "EOD chart's rightmost is yesterday, not today"

→ The cloud daily pipeline failed or didn't run, OR my local upload clobbered the cloud's HTML. Check:
- Most recent execution: `gcloud run jobs executions list --job=market-breadth-job --region=asia-southeast1 --limit=3`. Status should be `Completed = True`.
- Latest CSV on GCS: `gsutil cat gs://vn-market-breadth/intraday/combined_dataset.csv | head -1` (just verifies it's reachable). Local pull + check last_date — should include today.
- Live HTML's actual last X label: extract via curl + regex (see `INTRADAY_BREADTH.md`'s diagnostic section).

Fix: rebuild HTML locally and upload (verb above).

### "Intraday T-1 anchor doesn't match EOD chart's penultimate column"

→ Universe drift. Both must compute over **top-100 from `tickers.csv` only** (not the broader rs_fixed_tickers). Check `_build_eod_prices_frame` in `intraday_breadth.py` filters to `top100_set`, and that `market_breadth.py:main()` pre-filters `price_data` before calling `calculate_breadth`. See [UNIVERSES.md](UNIVERSES.md).

### "Intraday job runs but `eod_history` is empty"

→ The job is running an old image that doesn't have `compute_eod_breadth_series`. Check `gcloud run jobs describe intraday-breadth-job --format="value(...containers[0].image)"`. If it's pinned to an older digest than `:latest`, repin (verb above).

### "Cloud Build for a fresh push didn't fire"

→ Sometimes GHA fails silently; verify with `gcloud builds list --region=asia-southeast1 --limit=3` to confirm a build for the latest commit exists. If not, manually submit:
```bash
gcloud builds submit \
  --tag asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth:latest \
  --region asia-southeast1 \
  --project project-feb6df0e-9749-4925-b4e
```

### "Universe Drift banner showed up again on the dashboard"

→ The banner suppression in `market_breadth.py` (commit `dc5064a`) was overwritten or reverted. Search for `drift_notification_html = ""` near line 935 and confirm the conditional `if drift_payload and drift_payload["is_significant"]:` block is gone. The drift script (`rs_universe_generator.py`) still runs and writes `logs/universe_drift_*.txt` regardless — that's intentional for audit.

## Rotate the vnstock API key

```bash
echo -n '<new_key>' | gcloud secrets versions add vnstock-api-key --data-file=-
# Cloud Run mounts :latest, so the next execution picks up the new version automatically.
gcloud secrets versions disable <old_version> --secret=vnstock-api-key
```

## ⚠️ Don't run

- **`python rs_universe_generator.py --sync-universe`** — would wipe the 58 manual additions in `rs_fixed_tickers.csv`. See [UNIVERSES.md](UNIVERSES.md).
- **`gsutil rm gs://vn-market-breadth/intraday/combined_dataset.csv`** — would brick the intraday job AND `rs_matrix_3T.py` (which reads OHLC directly from this file post-`4a6b13a`) until the next 15:15 ICT pipeline rewrites it.
- **`git push --force` on master** — would rewrite published commits other clients (Cloud Build, GHA) reference.

## ⚠️ Recurring gotchas

### Dockerfile COPY list is explicit — new Python modules must be added

The `Dockerfile` does not `COPY . ./`. It enumerates each Python file by name:

```dockerfile
COPY run_daily_update.py \
     eod_batch_downloader.py \
     rs_universe_generator.py \
     rs_matrix_3T.py \
     rs_matrix_crypto.py \
     ...
     intraday_breadth.py \
     intraday_rs_3T.py \
     vnindex_ex_vin.py \
     ./
```

If you add a new Python module that ANY entrypoint imports, you must also add it to the `Dockerfile` COPY list. The signature failure is `ModuleNotFoundError: No module named 'X'` at the start of the next Cloud Run execution, with the daily pipeline aborting at exit code 1 (we tripped this twice: commit `ac44d58` adding `intraday_rs_3T.py`, then `5a691fc` adding `vnindex_ex_vin.py`).

When this happens you'll also see the dashboard go stale because the EOD pipeline never reaches the HTML-upload step.

### Cloud Run jobs pin `:latest` to a digest at update time

A `gcloud run jobs update --image ...:latest` resolves the tag to the current digest and stores that digest in the job spec. After a new build, the registry's `:latest` tag points elsewhere, but the *job* still points at the old digest until you `gcloud run jobs update` again.

If a fresh push doesn't seem to take effect (e.g., new env vars or code missing from the running container), check the digest:

```bash
gcloud run jobs describe market-breadth-job --region=asia-southeast1 \
  --project=project-feb6df0e-9749-4925-b4e \
  --format="value(spec.template.spec.template.spec.containers[0].image)"
```

GH Actions has a step that re-pins after every build; if that's failing, re-pin manually:

```bash
gcloud run jobs update market-breadth-job --region=asia-southeast1 \
  --project=project-feb6df0e-9749-4925-b4e \
  --image=asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth:latest
```

### Artifact Registry manual deletes need `--async`

`gcloud artifacts docker images delete <repo>@<digest>` (sync mode) starts the delete via a long-running operation, then polls `operations.get` to confirm completion. The polling step intermittently returns:

```
PERMISSION_DENIED: Permission denied on operation projects/.../operations/<UUID> (or it may not exist).
```

even when the calling account has `roles/owner`. Workaround: add `--async --quiet` to fire-and-forget. Eventual consistency takes a few minutes per delete to actually reduce the listing.

```bash
gcloud artifacts docker images delete <repo>@sha256:... --delete-tags --async --quiet --project=...
```

This is why the manual one-time AR prune (May 2026) didn't immediately free space — see [COST_PROTECTION.md](COST_PROTECTION.md). The cleanup policy is the reliable path for ongoing maintenance; the manual prune is only a way to short-circuit by ~24h.

### `eod_batch_downloader.py` does NOT drop today's partial bar

If you trigger `market-breadth-job` manually *during* VN trading hours (09:00–14:45 ICT) on a weekday, vnstock returns an in-progress row with today's date and `close = current intraday price`. That row leaks into `combined_dataset.csv` and the RS / breadth heatmaps render an `18-05`-style column that's actually an intraday snapshot, not a settled close. The 15:15 ICT scheduled run fixes this automatically (it fetches *after* the 14:45 close, so the bar is settled).

If you need a manual mid-session refresh, accept the partial-bar caveat or wait until after 15:00 ICT.
