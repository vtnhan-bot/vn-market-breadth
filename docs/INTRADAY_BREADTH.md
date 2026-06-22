# Intraday Market Breadth Chart

> **⚠ GCP note (updated 2026-06-21):** The `intraday-breadth-job` Cloud Run job is triggered by a **VM systemd timer** (`engine-intraday-breadth.timer` on the pattern-engine VM), **NOT Cloud Scheduler**. The "Cloud Scheduler → Cloud Run Job" pipeline diagram and the Cloud Scheduler row in the infra table below describe deleted infrastructure; the cron expression still reflects the actual cadence but the trigger is now a systemd timer.
> Canonical current state: this project's CLAUDE.md → "GCP Deployment & Cost Safety", and d:\Claude\Devops\ARCHITECTURE.md. Content below is kept for reference.

A live breadth chart that updates every 15 minutes during VN trading hours, displayed below the EOD breadth chart on the main dashboard.

## What you see on the chart

**X axis** — categorical strings, never date-parsed (Plotly `type: 'category'`):
- Left: 49 EOD daily labels in `DD-MM` format (e.g. `24-02`, `25-02`, …, `06-05` if T = today is 07-05).
- Right: 1 "latest tick" label whose value depends on time of day:
  - **`HH:MM` ICT** during the VN session (09:30, 09:45, …, 14:30) — the in-progress intraday tick written by `intraday-breadth-job`.
  - **`Đóng cửa`** outside the session (after the 15:15 ICT EOD run, throughout the overnight gap, and until the next 09:00 ICT tick rolls the day forward) — a synthetic close tick written by `market_breadth.py:refresh_intraday_breadth_json()` carrying today's EOD breadth values. Added in commit `39c68f2` to avoid the chart showing yesterday's stale `14:30` tick at 08:00 the next morning.

**Y axis** — `% trên SMA`, range 0–100, `%` suffix.

**Lines** — same colors and dash pattern as the EOD chart, six MA periods:
- mbz03 cyan dotted • mbz05 orange dotted • mbz10 green solid • mbz20 purple solid • **mbz50 black bold (width 4)** • mbz200 red solid.

**Status text above the chart** — "Cập nhật lúc HH:MM DD/MM/YYYY (giờ Việt Nam, ngày YYYY-MM-DD)" + "49 phiên EOD + 1 điểm intraday (HH:MM)".

## Data contract

Total chart points = **49 EOD + 1 intraday = 50**.

For each MA period N ∈ {3, 5, 10, 20, 50, 200}:
- **SMA reference is built from the last N closed EOD daily closes ending T-1** (yesterday). It is frozen for the entire day — never recomputed during intraday.
- **EOD points** (49 days from T-49 to T-1): each ticker's value is `1 if close[D] > SMA-N(D) else 0`, percentage across the universe. Exactly identical to the top EOD breadth chart's calculation at any past date.
- **Intraday point** (today): each ticker's value is `1 if intraday_price(now) > SMA-N(T-1) else 0`, percentage across the same universe. The SMA does not move; only the price does.

## Universe

**Top-100 from `tickers.csv`** — same as the EOD breadth chart. **NEVER** the broader 230-ticker `rs_fixed_tickers.csv` (that's for RS / pre-breakout, not breadth). See [UNIVERSES.md](UNIVERSES.md).

## Pipeline

```
Cloud Scheduler ─ */15 9-14 * * 1-5 ICT ─▶ Cloud Run Job (intraday-breadth-job)
                                              │
                                              ├── Time-window check: 09:30–11:30 / 13:00–14:45 ICT
                                              │   (no-op silently outside; 09:00, 09:15, lunch breaks etc.)
                                              ├── Pull gs://vn-market-breadth/intraday/combined_dataset.csv
                                              ├── Build top-100 EOD prices frame, drop today's row if present
                                              ├── Compute 49-day EOD breadth series + 1 intraday tick
                                              ├── ssi_client.get_current_prices() — ~1 SSI intraday_ohlc call/ticker @ ~1 req/s, ~120s for 100 tickers
                                              └── Write gs://vn-market-breadth/intraday_breadth.json (cache-busted)

Browser  ──▶ fetch intraday_breadth.json every 60s ──▶ Plotly.react()
```

## Sibling: intraday RS update

At the end of every successful intraday breadth tick, `intraday_breadth.py:main()` also calls `intraday_rs_3T.run_intraday_rs(now_ict, combined_local)`. That step publishes a separate `gs://vn-market-breadth/intraday_rs_3T.json` and the dashboard JS uses it to prepend a `HH:MM` column to the RS heatmap. Failure of the RS step is non-fatal — the breadth tick still publishes.

See [`INTRADAY_RS.md`](INTRADAY_RS.md) for the full RS contract.

## JSON schema (`gs://vn-market-breadth/intraday_breadth.json`)

```jsonc
{
  "date": "2026-05-07",
  "last_updated_ict": "14:45 07/05/2026",
  "eod_history": [
    // 49 entries; index 0 is the oldest (T-49), last is T-1 (yesterday).
    {
      "kind": "eod",
      "date": "2026-02-24",
      "time": "24-02",                   // X-axis label
      "mbz3": 35.0, "mbz5": 32.0, "mbz10": 28.0, "mbz20": 33.0, "mbz50": 41.0, "mbz200": 47.0,
      "sample_size": 99
    },
    // ...
    {
      "kind": "eod",
      "date": "2026-05-06",              // T-1 (rightmost EOD point)
      "time": "06-05",
      "mbz3": 73.47, "mbz5": 69.39, "mbz10": 58.16, "mbz20": 53.06, "mbz50": 48.98, "mbz200": 44.09,
      "sample_size": 99
    }
  ],
  "updates": [
    // Every intraday tick today, oldest first; the JSON keeps all 17 ticks today
    // for audit/forensics, but the chart only renders updates[updates.length - 1].
    {
      "kind": "intraday",
      "time": "09:30",
      "timestamp_ict": "2026-05-07 09:30:11 +0700",
      "mbz3": 60.2, "mbz5": 58.16, "mbz10": 55.10, "mbz20": 45.92, "mbz50": 46.94, "mbz200": 48.39,
      "sample_size": 99
    },
    // ...up to 17 ticks per day (09:30, 09:45, ..., 14:45)
  ]
}
```

The JS reads `eod_history.slice(-49).concat([updates[updates.length - 1]])` — defensive slice so the chart stays at exactly 50 points even if `eod_history` ever gets longer.

### Who writes the JSON when

| Writer | When | What it writes |
|---|---|---|
| `intraday_breadth.py` (every 15 min cron during 09:30–14:45 ICT) | Inside trading window | Appends current `HH:MM` tick to `updates[]`. On the first tick of a new day (`existing.date != today_str`), resets the JSON: `date=today`, `eod_history=` last 49 EODs ending T-1, `updates=[09:00 tick]`. |
| `market_breadth.py:refresh_intraday_breadth_json()` | End of every EOD run (15:15 ICT scheduled or any ad-hoc trigger) | Overwrites the JSON to a "post-close" state: `date=today_just_closed`, `eod_history=` last 49 EODs ending T-1, `updates=[{time:"Đóng cửa", …today's EOD breadth values}]`. This makes the chart's rightmost column read `Đóng cửa` from 15:15 ICT today through tomorrow's 09:00 first intraday tick. |

Conflict avoidance: the EOD writer only fires once per day at/after 15:15 ICT (after the intraday cron has stopped at 14:45). The intraday cron is the sole writer 09:00–14:45 ICT. They don't race.

**mbz key format gotcha**: `intraday_breadth.py` and the EOD writer both produce keys like `mbz3`, `mbz5`, `mbz10`, `mbz20`, `mbz50`, `mbz200` (no zero-padding for single digits). `market_breadth.py`'s breadth DataFrame uses `mbz03`, `mbz05` internally — `refresh_intraday_breadth_json` strips the leading zero when copying values across. Don't normalize either side without updating the other.

## Cloud infrastructure

| Resource | Config |
|---|---|
| Cloud Run Job | `intraday-breadth-job`, region `asia-southeast1`, image `:latest`, command override `python3 intraday_breadth.py`, cpu=1, mem=512Mi, timeout=720s, max-retries=1 |
| Env vars | `GITHUB_ACTIONS=true`; `SSI_FC_DATA_CONSUMER_ID` + `SSI_FC_DATA_CONSUMER_SECRET` from Secret Manager (current-price source, see below); `VNSTOCK_API_KEY` from `vnstock-api-key:latest` (legacy — no longer used by intraday breadth) |

> **Timeout note (2026-06-22):** raised 120s → 600s when the current-price source moved off vnstock onto SSI FastConnect. SSI has no batch price-board, so `fetch_current_prices()` (in `ssi_client.py`) makes ~one `intraday_ohlc` REST call per ticker, rate-limited to SSI's ~1 req/sec → a full 100-ticker sweep runs ~120s and was hitting the old 120s wall. vnstock's `Trading.price_board()` started returning HTTP 403 from the cloud on 2026-06-22; SSI is reachable from GCP (the VN trading-signal engine uses it).
| Cloud Scheduler | `intraday-breadth-schedule`, cron `*/15 9-14 * * 1-5`, time-zone `Asia/Ho_Chi_Minh`, OAuth via `github-deploy@…` |

The job auto-refreshes its `:latest` digest after every Cloud Build via the GHA workflow's "Update intraday Cloud Run Job image" step.

## Trading-window logic (in `is_trading_window()`)

| Time (ICT) | Behaviour |
|---|---|
| Before 09:30, weekday | No-op (cron may fire 09:00, 09:15 — ignored) |
| 09:30 → 11:30 | Compute and append tick |
| 11:31 → 12:59 (lunch break) | No-op |
| 13:00 → 14:45 | Compute and append tick |
| After 14:45, weekday | No-op (no scheduled fires after 14:45 anyway) |
| Saturday / Sunday | Cron explicitly mask `* * 1-5` — never fires |

The `INTRADAY_FORCE=1` env var bypasses the time-window check. Used for one-off manual triggers (re-seeding after a fix, smoke testing). Always remove the env var afterward to prevent unintended off-hours computes.

## Local testing

```bash
# Dry run with the locally-cached EOD CSV, bypass time window:
INTRADAY_DRY_RUN=1 \
INTRADAY_FORCE=1 \
INTRADAY_LOCAL_COMBINED=data/2026-05-07/combined_dataset.csv \
.venv/Scripts/python.exe intraday_breadth.py
```

Logs the computed breadth without uploading to GCS. Useful when iterating on the formula.

## Common operational situations

**The intraday chart's T-1 anchor doesn't match the EOD chart's T-1 column.** → universe drift. Both must use top-100 from `tickers.csv` only. Check `_build_eod_prices_frame()` filters to `top100_set`. Re-run script.

**The intraday chart shows multiple ticks today (09:30, 09:45, 10:00…).** → the JS isn't filtering to the last update. Check `renderIntradayBreadth` uses `updates[updates.length - 1]`, not `concat(updates)`.

**The intraday job runs but `eod_history` is empty.** → image is stale. Verify with `gcloud run jobs describe intraday-breadth-job --format="value(spec.template.spec.template.spec.containers[0].image)"`. If on `:latest` but registry has a newer digest than what the job resolved at update-time, manually pin to digest:
```bash
LATEST=$(gcloud artifacts docker tags list asia-southeast1-docker.pkg.dev/project-feb6df0e-9749-4925-b4e/market-repo/market-breadth --filter="tag.basename()=latest" --format="value(version)")
gcloud run jobs update intraday-breadth-job --region=asia-southeast1 --image=…/market-breadth@$LATEST
```
Then switch back to `:latest` once verified, so future GHA updates flow through.

**`combined_dataset.csv` on GCS is stale (eod_history's T-1 is more than 1 trading day behind).** → tonight's daily pipeline at 15:30 ICT will refresh it (entrypoint.sh persists it after each run). To unstick mid-day: pull cloud-side and re-upload, OR run `eod_batch_downloader.py` locally and `gsutil cp` the result to `gs://vn-market-breadth/intraday/combined_dataset.csv`.

**Dashboard HTML stale after a manual upload clobbered the cloud's run.** → re-pull `gs://vn-market-breadth/intraday/combined_dataset.csv` (the cloud always persists a fresh one), then `python market_breadth.py --no-browser && gsutil cp market_breadth.html gs://vn-market-breadth/index.html`. Tomorrow's 15:30 ICT cloud run will republish naturally.
