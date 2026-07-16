# Stale-Dashboard Fix, Cache Headers & Cost Reality — Session Handoff (2026-07-17)

Durable, version-controlled record of the 2026-07-17 session. Read alongside
[`../PROJECT_KB.md`](../PROJECT_KB.md) and [`SSI_MIGRATION.md`](SSI_MIGRATION.md). For the AI-side
restart context see the memory files under
`C:\Users\DELL\.claude\projects\d--Claude-Market-on-website\memory\` (start with
`project_current_status.md`).

---

## 1. The bug: the dashboard was one trading day stale every Tue–Fri

**Symptom.** On 2026-07-16 the user reported "no update for the trading day of July 16". The 15:15 ICT
EOD run had fired on time, exited **0**, logged `SUCCESS: Dashboard Ready`, and rewritten every
artifact at 15:16 — with **July 15's data**.

**Root cause — two independent defects that only bite together:**

1. **The EOD cache is keyed by ICT calendar date** (`data/<YYYY-MM-DD>/`), and `fetch_with_retry`
   short-circuited on nothing but `if cache_path.exists()`. The **07:30 Tue–Sat** run (added at the
   2026-06-27 VM migration, to refresh US macro after the US close) populated `data/<today>/` with
   bars through **T‑1** — correct at 07:30, since VN has not traded yet. At 15:15 the EOD run computed
   the *same* dir, found all 231 ticker CSVs present, and logged `ACB loaded from cache` ×231 —
   **zero network fetches** — then rebuilt the dashboard from the 07:30 snapshot.
2. **`verify_fresh_eod_dataset` validated the file's mtime, not its content.** The 15:15 run rewrites
   `combined_dataset.csv` unconditionally, so the mtime always looked fresh. The guard passed and
   printed `Verified fresh EOD dataset` on a byte-for-byte republish of a pre-close snapshot.

**Why it hid for ~2.5 weeks.** `load_cached_ticker` re-emits the original `fetched_at` and `source`,
so a 100%-cached run was indistinguishable from a healthy one in every other log line. The published
artifact proved it outright: `index.html` uploaded 15:16 ICT contained rows stamped
`fetched_at=2026-07-16T07:34:45`.

**Why only Tue–Fri.** The timer is `Mon-Fri 15:15` + **`Tue-Sat 07:30`**. Monday has no 07:30 run, so
Monday's 15:15 found a cold cache and fetched for real. Observed:

| Day | 07:30 run? | 15:15 wrote tick for | |
|---|---|---|---|
| Jul 13 (Mon) | no | **Jul 13** | correct |
| Jul 14 (Tue) | yes | Jul 13 | stale |
| Jul 15 (Wed) | yes | Jul 14 | stale |
| Jul 16 (Thu) | yes | Jul 15 | stale |

**Why the VM migration exposed it.** On Cloud Run every execution got a fresh container filesystem, so
the cache dir was *always* empty and the code always fetched. Moving to a **persistent local cache**
on the VM (the deliberate "cache LOCAL, no GCS round-trip" change) turned the existence check into a
silent no-op fetch. Net effect: EOD data effectively landed ~16 h late, at the *next* morning's 07:30.

## 2. The fix

### 2.1 `eod_batch_downloader.py` — content-aware cache reuse

`cache_entry_is_current(cached_df, end_date, now_ict)`:

- **Pre-close runs** (`now_ict.hour < 15`) may reuse any same-day entry — keeps 07:30 retries cheap.
- **Post-close runs** may only reuse an entry that **already carries today's bar**; anything older is
  refetched (~6 min for 231 tickers, against `TimeoutStartSec=1800`).

This keeps same-slot retries cheap (a 15:40 rerun reuses the 15:15 fetch, ~12 s) while making it
impossible for a pre-close snapshot to survive into a post-close publish. **Deliberately no calendar
lookup**: the repo has no VN holiday data, and on a holiday no entry ever carries today's bar, so a
post-close run simply refetches and gets the same last-trading-day data back — correct, just not free.

Also: a failed refetch now falls back to its cached frame as status **`stale_cache`** rather than
dropping the ticker — dropping would shrink the universe below `MIN_SUCCESSFUL_TICKERS` and abort an
otherwise publishable run. `fetched_at` is now **tz-aware**; `get_today_cache_dir()` uses explicit ICT
(matching `archive_previous_day_cache` and `market_breadth.get_today_combined_dataset_path`); and a
`fetched= / cached= / stale_cache= / failed= | newest bar=` **run summary** now makes "did this run
actually fetch?" answerable from the log.

### 2.2 `market_breadth.py` — content-based freshness guard

`verify_fresh_eod_dataset()` strict (post-close) branch now **aborts** when:

- the newest `fetched_at` **predates today's 15:00 ICT close** — the reheated-snapshot signature; or
- **< 80 %** (`MIN_LATEST_SESSION_COVERAGE`) of the **top-100 breadth universe** carries the dataset's
  newest session — a partial refetch.

It **warns and publishes** when `latest_session < today`. This is deliberate: VN holidays and upstream
source lag are **indistinguishable** without a trading calendar; the chart's `Dữ liệu mới nhất` label
is derived from the data itself so it stays truthful; and aborting would also freeze the US macro and
crypto RS panels that *are* legitimately fresh on a VN holiday.

> **Coverage is measured over `tickers.csv` (top-100), not the ~230-name RS universe.** An 80 % floor
> over 231 tickers is only ~54 % over the 100 actually plotted. This was a real defect found by
> adversarial review of the first cut of this fix.

### 2.3 Invariants to preserve

- `POST_CLOSE_SETTLE_HOUR = 15` is duplicated in `eod_batch_downloader.py` and `market_breadth.py`.
  **Keep them in step.**
- The **pre-15:00 permissive branch** of the guard exists because the 07:30 run legitimately has no
  same-day VN bar. Do not collapse it into the strict branch.
- Invalidate the cache by **not reading it**, never by deleting it. `data/<today>/` is also read by
  `rs_matrix_3T.py`, `pre_breakout.py`, `run.sh`'s publish and the next morning's intraday ticks.

### 2.4 Verification performed

Replayed against the **real** dataset published on 2026-07-16, the guard aborts with:

```
CRITICAL: EOD dataset was last fetched at 16/07/2026 07:36 ICT, before today's 15:00 close.
A pre-close snapshot is being republished as today's data. Aborting HTML Update.
```

10 checks pass: the real stale publish aborts; a healthy day publishes (98 % coverage); a simulated
holiday publishes with an honest warning; a partial refetch aborts; a **concentrated top-100 outage**
aborts (58 % top-100 coverage even though dataset-wide reads 81.4 %); and the 07:30 / 15:15 / 15:40 /
14:59 / 15:00 cache slots all behave. Confirmed in-situ on the VM against `data/2026-07-16/`.

## 3. Cache headers: `no-store` → `no-cache, must-revalidate`

`index.html` is **800,798 bytes** and is rebuilt **only twice a day** (the intraday timer republishes
only the small JSONs), but `no-store` forbade *storing* it, forcing a full re-download on **every**
page load. GCS honours `If-None-Match`:

| | status | bytes | time |
|---|---|---|---|
| Full GET (before) | 200 | 800,798 | 0.523 s |
| Conditional GET (after) | **304** | **0** | **0.166 s** |

Freshness is unchanged — `no-cache` still forces revalidation before any reuse. Changed in `run.sh`
plus the three `blob.cache_control` sites (`market_breadth.py`, `intraday_breadth.py`,
`intraday_rs_3T.py`). **Not changed:** the page's own JS cache-busts the JSONs with
`?_=Date.now()` + `cache:'no-store'` — correct for live tick data. `entrypoint.sh` still has
`no-store` but is the **dead Cloud Run path**.

## 4. Cloudflare R2 dual-publish leg (staged, not live)

`r2_publish.py` — S3-compatible put to R2. **Inert** until `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and
`R2_SECRET_ACCESS_KEY` all exist; `boto3` is imported **lazily** so a missing dep is treated exactly
like "not configured". Wired into the 3 Python upload sites and `run.sh` (`r2pub()` helper, `|| true`,
never touches `$fail`). GCS stays primary until cutover. Chosen public URL: **r2.dev** dev URL.

> **Deployment state:** the R2 leg is in the repo but **deliberately NOT on the VM**. Wiring `r2pub`
> restructured `run.sh`'s three `gsutil` blocks from `&&`/`||` one-liners into `if/then/else` — syntax
> checked but unrun. The VM keeps the simpler header-only `run.sh`. **Repo and VM diverge by exactly
> that inert R2 leg.**

## 5. Cost reality — three corrections to prior docs

1. **There is NO 2026-07-18 "cliff".** The account **already converted Free Trial → paid ~2026-07-01**
   (Always Free only applies to *paid* accounts; Regional Class A free ops = **0 in June** vs
   **exactly 5,000 in July**; Class B 0 → 50,000). On 07-18 nothing is suspended or deleted — the
   leftover promo `FreeTrial:Credit-017EA5-270660-A8352F` simply runs out and ≈**60,000 VND/mo
   (~$2.30)** starts hitting the card. The e2-micro survives regardless (negative-cost free-tier SKUs).
2. **GCS Always Free ops are a SHARED cross-region pool, already 100 % exhausted** — not per-region.
   202607: Class A zero-rated = `asia-southeast1 1,143.52 + us-central1 3,856.48 = exactly 5,000.0`;
   Class B `1,128.73 + 48,871.27 = exactly 50,000.0`. So `gs://vn-market-breadth` **already receives
   its pro-rata free ops in Singapore**, and marginal prices are identical in both regions.
   Only **storage** free tier is US-region-restricted. → **Moving the bucket to us-central1 saves
   5.46 VND/mo (~$0.0002) and is a NO-GO** — it also costs a **measured 2.53×** VN latency regression.
3. **`gcp-stop-jul13` does not exist** in any of the 27 scheduler locations. The documented
   "auto-unlink billing on 2026-07-13" backstop is **not armed**. Scheduler count is 0 of 3.

**Billing-export method trap:** the GCS free tier arrives as a **zero-rated price tier**
(`price.tier_start_amount = 0` → `effective_price = 0` → `cost = 0.0`), **not** as a `FREE_TIER` credit
row and **not** as a negative-cost SKU (that pattern is Compute Engine only). Always split by
`price.tier_start_amount` via `UNNEST([price]) p`. A naive `cost + credits` query reports "zero free
tier" and is wrong — this produced a false finding earlier in this session.

## 6. Cointrading Class B runaway (different engine, same VM/bucket bill)

The `CHANGELOG.md:56` claim — *"cointrading-state-sync.timer → ~89k us-central1 Class A ops/mo"* — is a
**misattribution**. Those 89k Class A ops were a **3-day burst (Jun 19–21)** from the dying Cloud Run
`entrypoint.sh` doing per-object restore/persist during the VM migration (~1:1 A:B ratio = the
signature). It **self-resolved on Jun 22**; `sync_state.sh` didn't exist until Jun 25 and only ever did
~165 Class A/day.

The real driver was **Class B**: `sync_one()` made **3 metadata GETs per file per cycle**
(`blob.reload()` with the result discarded, `blob.exists()`, then `blob.reload()` again) on 548 files ×
96 cycles/day, regardless of change. Measured: 9,922/day (Jun 25) → 28,626 (Jul 6, when `taker_split`
was added) → **168,616/day (Jul 15)**, growing ~14.5k/day-over-day because `taker_split` gains ~36
files/day with no pruning. ~$2.73/mo → ~$7.70/mo projected by end of August.

**Fixed** (`d:\Claude\Cointrading\deploy\vm\sync_state.sh`): one `tar -czf` → one unconditional PUT.
**1 Class A, 0 Class B per cycle** → ~2,880/mo, back inside the 5,000 free allowance. Deployed and
verified restorable (download → extract → nested `taker_split/<TICKER>/<date>.parquet` intact).

> **Gotcha found only by running it:** GNU tar exits **1** on `file changed as we read it` — the trade
> collector appends to `taker_split/*/recent_trades.parquet` continuously — and `set -e` turned that
> into a hard failure on **every** cycle. Now tolerated explicitly (exit 1 = warning, ≥2 = fatal) with
> a `tar -tzf` integrity gate before upload.

Original backed up on the VM at `sync_state.sh.pre-tar-20260717.bak`. The old per-object `cache/*`
backup (596 objects) is **intentionally left in place** as the recovery target until the tarball is
proven over time.

## 7. Open threads

- **Verify** the 15:15 ICT run refetches (`fetched=231 cached=0`, newest bar = run date), and that
  us-central1 Class B collapses from ~168k/day toward ~0 (billing export lags ~24 h).
- **R2 cutover** — blocked on a Cloudflare account: needs Account ID + Access Key ID + Secret, then the
  `pub-*.r2.dev` URL.
- **`taker_split` retention** — the tar fix makes op count flat, but the archive grows ~36 files/day.
  Mirror `PRUNE_KEEP_DAYS` (`tb_collector.py:123-125`) for the parquets. Needs a chosen window.
- **Cointrading has no VCS** and its local copy **diverges from the VM** (the VM had
  `publish_dashboard.sh`, absent locally). Reconcile against the VM before `git init`.
- **Nothing pages on failure.** A guard raise just means `run.sh` skips the publish and last-good stays
  live; detection is still "user notices a stale `Last-Modified`".
- **CI/CD** (`.github/workflows/update_chart.yml`) still broken since ~May 2026; all deploys manual.

## 8. Rollback

Pre-deploy copies of every file changed on the VM this session:
`/opt/market-breadth/.rollback-20260717/` (`eod_batch_downloader.py`, `market_breadth.py`,
`intraday_breadth.py`, `intraday_rs_3T.py`, `run.sh`).
Cointrading: `/opt/cointrading/Cointrading/deploy/vm/sync_state.sh.pre-tar-20260717.bak`.
