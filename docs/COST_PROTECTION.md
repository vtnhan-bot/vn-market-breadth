# Cost Protection

Three-layer cost guard on the breadth project. All driven off a single Pub/Sub topic.

## At a glance

| Layer | Resource | What it does |
|---|---|---|
| 1. Budget alerts | Budget `Market dashboard 120000 VND cap (approx 4.65 USD)` on billing account `017EA5-270660-A8352F` | Monthly cap **120,000 VND** (~$4.65). Thresholds **60% / 80% / 90% / 100% / 120%**. Each crossing publishes a JSON message to Pub/Sub topic `billing-budget-alerts`. Budget alone is advisory — it does NOT cap spend by itself. |
| 2. Auto-killswitch | Cloud Function `billing-killswitch` (`stop_billing`), `asia-southeast1`, Python 3.11 | Subscribed to the topic. When `costAmount > budgetAmount` (100% threshold), unlinks the project from billing → halts every chargeable resource. Project goes dark until manually re-linked. |
| 3. Telegram alert | Cloud Function `telegram-budget-alert` (`telegram_alert`), `asia-southeast1`, Python 3.11, 128Mi | Subscribed to the same topic. Filters to `alertThresholdExceeded == 0.8` (only the 80% threshold). Posts Markdown to chat `1465938300` via `@SuperGemini_bot`. Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALERT_THRESHOLD=0.8`. |

```
GCP Billing Budget ─ threshold crossed ─▶ Pub/Sub: billing-budget-alerts
                                              │
                       ┌──────────────────────┴─────────────────────┐
                       ▼                                            ▼
              billing-killswitch                          telegram-budget-alert
              (if cost > budget)                          (if alertThresholdExceeded == 0.8)
                       │                                            │
                       ▼                                            ▼
            unlink project from                          POST sendMessage to
            billing → resources halt                     api.telegram.org/bot…
```

## Resource coordinates

| Item | Value |
|---|---|
| Billing account (linked) | `017EA5-270660-A8352F` |
| Budget ID | `88229f36-a2d9-41b2-ba41-4646dd92c709` |
| Pub/Sub topic | `projects/project-feb6df0e-9749-4925-b4e/topics/billing-budget-alerts` |
| Killswitch function | `billing-killswitch`, entry point `stop_billing`, region `asia-southeast1`, SA `746287134716-compute@developer.gserviceaccount.com` |
| Telegram function | `telegram-budget-alert`, entry point `telegram_alert`, region `asia-southeast1`, SA `746287134716-compute@developer.gserviceaccount.com` |
| Telegram bot | `@SuperGemini_bot` (id `8651375825`) |
| Telegram chat ID | `1465938300` |

⚠️ **Easy mistake**: there are two billing accounts visible in this gcloud profile — `017EA5-270660-A8352F` ("My Billing Account") and `013368-B9CB5C-10D9BA` ("Vietnam market update"). The breadth project is linked to the **first**. The second has no budgets.

## Budget alert payload schema (v1.0)

```json
{
  "budgetDisplayName": "Market dashboard 120000 VND cap (approx 4.65 USD)",
  "alertThresholdExceeded": 0.8,
  "costAmount": 96000,
  "costIntervalStart": "2026-05-01T00:00:00Z",
  "budgetAmount": 120000,
  "budgetAmountType": "SPECIFIED_AMOUNT",
  "currencyCode": "VND"
}
```

Periodic spend updates (publish whenever the budget service refreshes its cost estimate) arrive WITHOUT `alertThresholdExceeded`. Both functions skip those.

## Killswitch behavior

`billing-killswitch:stop_billing` (from `gs://gcf-v2-sources-746287134716-asia-southeast1/billing-killswitch/function-source.zip`):

```python
def stop_billing(event, context):
    pubsub_data = base64.b64decode(event["data"]).decode("utf-8")
    payload = json.loads(pubsub_data)
    cost_amount = payload.get("costAmount", 0)
    budget_amount = payload.get("budgetAmount", 0)
    if cost_amount <= budget_amount:
        return  # within budget, no action
    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    projects = billing.projects()
    info = projects.getBillingInfo(name=PROJECT_NAME).execute()
    if info.get("billingEnabled"):
        projects.updateBillingInfo(
            name=PROJECT_NAME,
            body={"billingAccountName": ""},
        ).execute()
```

It compares actual cost vs budget on every message — runs at every threshold publish, but only takes action above 100%.

## Telegram alert behavior

`telegram-budget-alert:telegram_alert`:

```python
threshold = payload.get("alertThresholdExceeded")
if threshold is None:
    return  # periodic spend update, no threshold crossed
if abs(float(threshold) - ALERT_THRESHOLD) > 0.001:
    return  # other threshold (60/90/100/120), not 80
# send Markdown message to Telegram
```

Message format:

```
🚨 GCP Budget 80% reached

Budget: Market dashboard 120000 VND cap (approx 4.65 USD)
Spent: 96,000 VND of 120,000 VND (80.0%)
Period start: 2026-05-01T00:00:00Z
Notified at: HH:MM DD/MM/YYYY ICT

Killswitch fires at 100% — billing will unlink automatically. Re-link via:
gcloud billing projects link project-feb6df0e-9749-4925-b4e --billing-account=017EA5-270660-A8352F
```

Source under `C:/Users/DELL/AppData/Local/Temp/telegram-budget-alert/` (rebuild + redeploy from there if changing).

## Restore billing after a killswitch fire

```bash
gcloud billing projects link project-feb6df0e-9749-4925-b4e \
  --billing-account=017EA5-270660-A8352F
```

That's it — billing reattaches and Cloud Run / Cloud Build / Pub/Sub resume in minutes. This engine's scheduled runs are now driven by VM systemd timers (`engine-market-breadth.timer` / `engine-intraday-breadth.timer`) on the always-on pattern-engine VM, NOT Cloud Scheduler; the next timer fire picks up automatically.

## Verbs

### Inspect the budget

```bash
gcloud billing budgets list \
  --billing-account=017EA5-270660-A8352F \
  --billing-project=project-feb6df0e-9749-4925-b4e
```

### Add or modify a threshold

```bash
ACCESS_TOKEN=$(gcloud auth print-access-token)
curl -s -X PATCH \
  "https://billingbudgets.googleapis.com/v1/billingAccounts/017EA5-270660-A8352F/budgets/88229f36-a2d9-41b2-ba41-4646dd92c709?updateMask=thresholdRules" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: project-feb6df0e-9749-4925-b4e" \
  -d '{"thresholdRules":[
        {"thresholdPercent":0.6,"spendBasis":"CURRENT_SPEND"},
        {"thresholdPercent":0.8,"spendBasis":"CURRENT_SPEND"},
        {"thresholdPercent":0.9,"spendBasis":"CURRENT_SPEND"},
        {"thresholdPercent":1.0,"spendBasis":"CURRENT_SPEND"},
        {"thresholdPercent":1.2,"spendBasis":"CURRENT_SPEND"}
      ]}'
```

The `X-Goog-User-Project` header is mandatory because this gcloud profile's quota-project defaults to `vtnhan-chess` (Cloud Billing API disabled there). Without it you'll see a misleading `PERMISSION_DENIED / SERVICE_DISABLED`.

### Simulate a budget alert (smoke test)

```bash
gcloud pubsub topics publish billing-budget-alerts \
  --project=project-feb6df0e-9749-4925-b4e \
  --message='{"budgetDisplayName":"Market dashboard","alertThresholdExceeded":0.8,"costAmount":96000,"budgetAmount":120000,"currencyCode":"VND","costIntervalStart":"2026-05-01T00:00:00Z"}' \
  --attribute="schemaVersion=1.0"
```

Then check function logs:

```bash
gcloud functions logs read telegram-budget-alert \
  --region=asia-southeast1 --project=project-feb6df0e-9749-4925-b4e --limit=10
```

You should see `Telegram send OK: …` and a real DM in Telegram.

### Rotate the bot token

If the token leaks (e.g. it was pasted into a chat), rotate it:

1. DM `@BotFather` on Telegram → `/revoke` → pick `@SuperGemini_bot` → confirm.
2. `/token` to generate a fresh one.
3. Update the function:
   ```bash
   gcloud functions deploy telegram-budget-alert \
     --gen2 --region=asia-southeast1 --project=project-feb6df0e-9749-4925-b4e \
     --update-env-vars=TELEGRAM_BOT_TOKEN=<new_token>
   ```

The function source doesn't change — only the env var.

### Change which threshold fires Telegram

```bash
gcloud functions deploy telegram-budget-alert \
  --gen2 --region=asia-southeast1 --project=project-feb6df0e-9749-4925-b4e \
  --update-env-vars=ALERT_THRESHOLD=0.9
```

Same for `TELEGRAM_CHAT_ID` if you want alerts to a different chat/group.

## Cost optimization (May 2026 baseline)

### May 2026 bill: ~62,000 VND

First materially non-zero month since the budget went in. Resource inventory pulled at month end (2026-06-01):

| Resource | Usage in May | Free tier | Estimated VND |
|---|---|---|---|
| Cloud Run jobs total runtime | 914 min (550 executions across 3 jobs, ~110K vCPU-sec / 110K GiB-sec) | 240K vCPU-sec, 450K GiB-sec | ~0 |
| Cloud Build | 88 builds × ~1.5 min = 135 build-min | 120 build-min / day | ~0 |
| **Artifact Registry storage** | **7.55 GB across 49 image versions** (one per push) | 0.5 GB | **~18,500** (biggest visible item) |
| Cloud Logging ingest | ~198K entries / ~180 MB | 50 GB | ~0 |
| GCS storage + egress | 10.4 MiB bucket; egress well under 100 GB | 100 GB | ~0 |
| Cloud Functions, Pub/Sub, Eventarc, Scheduler | trivial volumes | covered | ~0 |
| **Total estimated** | | | **~18,500** |

The ~43,500 VND gap between estimate and invoice is likely Cloud Audit Logs above the 50 GB band or cross-region egress on image pushes — visible only with BigQuery billing export enabled.

### Artifact Registry cleanup policy (applied 2026-06-01)

Two rules on `asia-southeast1-docker.pkg.dev/.../market-repo`:

```yaml
keep-last-5:           # KEEP — protects the 5 newest versions unconditionally
  mostRecentVersions:
    keepCount: 5
delete-older-than-7d:  # DELETE — removes anything not protected by KEEP, older than 7 days
  condition:
    olderThan: 604800s
```

Inspect / replace:

```bash
gcloud artifacts repositories describe market-repo \
  --location=asia-southeast1 --project=project-feb6df0e-9749-4925-b4e

gcloud artifacts repositories set-cleanup-policies market-repo \
  --location=asia-southeast1 --project=project-feb6df0e-9749-4925-b4e \
  --policy=path/to/policy.json
```

**Why `keepCount=5`**: KEEP rules always trump DELETE, so the 5 newest versions are protected regardless of age. That gives a 4–5 day rollback window (Cloud Run pins the digest of `:latest` at update-time, so we can roll back to "the digest from 3 pushes ago" without rebuilding from source). Going below 3 would risk a race where GH-Actions hasn't re-pinned yet but the policy already deleted the digest a running job needs.

**Expected impact**: storage drops from 7.55 GB → ~450 MB on the policy's next scheduled run (within ~24h). AR line item goes to **0 VND** next month.

### Manual prune did not free space

While applying the policy, an attempt at a one-time manual delete (84 individual deletes) failed to actually free storage despite all initiating successfully. Root cause is the `--async` polling gotcha documented in [OPERATIONS.md](OPERATIONS.md): the sync delete needs `artifactregistry.operations.get` to confirm completion, and the IAM check in that path fails intermittently even for `roles/owner`. Async fires the delete but doesn't confirm, and AR's eventual consistency held the listing at 47 versions for the verification window.

Policy-driven cleanup is the reliable path; the one-time prune was only meant to short-circuit Google's scheduled cleanup by ~24h and isn't worth more effort.

### What to verify after the policy runs

Roughly 24h after policy application:

```bash
# Should drop to ~5 versions and ~450 MB
gcloud artifacts repositories describe market-repo \
  --location=asia-southeast1 --project=project-feb6df0e-9749-4925-b4e | grep "Repository Size"

gcloud artifacts docker images list \
  asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth \
  --include-tags --sort-by="~UPDATE_TIME" --format="value(version)" | wc -l
```

If size is still ~7.5 GB after 48h: the policy didn't run. Re-check `cleanupPolicies` is still in the repo describe output, and consider re-applying. If size dropped but is still above 0.5 GB: a recent flurry of pushes left more than 5 within the 7-day window — expected.

### Other tunable levers (not applied)

- **Cloud Logging retention** drop 30 → 7 days saves ~1–3K VND but loses audit-log forensics > 1 week old. Apply with `gcloud logging buckets update _Default --location=global --retention-days=7 --project=...`.
- ~~**`us-market-breadth-cron` scheduler**~~ — **DELETED 2026-06-21** (with `us-market-breadth-daily`). The us-market-breadth Cloud Run job is now triggered by the VM systemd timer `engine-us-market-breadth.timer` (16:30 + 17:30 ET); there is NO scheduler to pause — to change cadence edit the VM timer. Cloud Scheduler now holds only the one-time `gcp-stop-jul13`.
- **Intraday cron** from `*/15` → `*/30` halves intraday refreshes but yields ~0 VND (already in free tier) and degrades the [INTRADAY_RS.md](INTRADAY_RS.md) feature.

## Don't do

- **Don't lower the budget below realistic monthly spend.** The killswitch will fire daily.
- **Don't widen the Telegram alert from 80% only without asking.** User explicitly picked single-threshold over a 60/80/90/100/120% ladder.
- **Don't soften the killswitch to "just warn" instead of unlinking.** The whole protection model depends on a hard stop. If a real outage demands keeping billing live during an incident, raise the budget amount (`gcloud --project=… billing budgets update …`) rather than weakening the killswitch.
- **Don't grant `roles/owner` to the killswitch SA.** It only needs `roles/billing.projectManager` on the linked billing account. Wider permissions = wider blast radius if the function ever has a bug.

## Cross-refs

- [`PROJECT_KB.md`](../PROJECT_KB.md) — overall project structure.
- [`OPERATIONS.md`](OPERATIONS.md) — manual triggers, logs, image refresh.
