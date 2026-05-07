# Operations Playbook

Common operational situations and how to handle them. All times in **Asia/Ho_Chi_Minh (ICT, UTC+7)**.

## Schedules currently running

| Schedule | Cron (ICT) | Triggers | Avg duration | Purpose |
|---|---|---|---|---|
| `market-breadth-schedule` | `15 15 * * 1-5` | `market-breadth-job` | ~13–15 min (finishes ~15:28) | Daily after-3pm pipeline: download fresh EOD, build matrices, regenerate HTML, upload to GCS, persist `combined_dataset.csv` + `rs_matrix_3T.csv` + `rs_matrix_crypto.csv` to `gs://vn-market-breadth/intraday/` |
| `intraday-breadth-schedule` | `*/15 9-14 * * 1-5` | `intraday-breadth-job` | ~10–30s | Every 15 min during VN trading hours; the script's own check no-ops outside 09:30–11:30 / 13:00–14:45 |

## Verbs you'll actually use

### Trigger the daily pipeline manually (after 15:00 ICT — freshness gate aborts before)

```bash
gcloud run jobs execute market-breadth-job \
  --project=project-feb6df0e-9749-4925-b4e \
  --region=asia-southeast1 \
  --wait
```

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
- **`gsutil rm gs://vn-market-breadth/intraday/combined_dataset.csv`** — would brick the intraday job until tonight's 15:30 ICT pipeline rewrites it.
- **`git push --force` on master** — would rewrite published commits other clients (Cloud Build, GHA) reference.
