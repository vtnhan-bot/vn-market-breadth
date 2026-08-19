# Data-Sanity + Logic Audit, RS Recency, VN Trading Calendar — Session Handoff (2026-08)

Durable record of the 2026-08 work on the VN breadth dashboard. Read alongside
[`../PROJECT_KB.md`](../PROJECT_KB.md), [`STALE_DASHBOARD_FIX.md`](STALE_DASHBOARD_FIX.md) (2026-07 handoff),
and [`SSI_MIGRATION.md`](SSI_MIGRATION.md). AI-side restart context:
`C:\Users\DELL\.claude\projects\d--Claude-Market-on-website\memory\` (start with `project_current_status.md`).

**Git:** all code is on **`master`**, HEAD `d51f0e9`, pushed, CI green. `/opt/market-breadth` on the
VM is in sync (deployed by hand — there is no CD). Rollbacks: `/opt/market-breadth/.rollback-20260717`,
`.rollback-20260802-rs`, `.rollback-20260816`.

---

## 1. RS heatmap "recency" profile — option C (commit `d75b862`)

The 1–99 RS Rating barely moved on fresh price action (GEX +6.76% only nudged 13→14) because the blend
was anchored to a 90-day relative-return term that, at 30% weight, still supplied ~74% of the effective
memory. Simulation on live data showed the real responsiveness lever is the **momentum window length**,
not the anchor weight. Chosen profile:

| Lever | Before | After (option C) |
|---|---|---|
| Momentum windows | 5/10/20 | **3/5/10** |
| Benchmark anchor lookback | 90d | **45d** |
| Anchor blend weight | 0.30 | **0.20** |
| Effective mean lookback | ~26 sessions | **~10 sessions** |
| Responsiveness (Δrating vs day's return) | 0.74 | **0.82** |

Tunables live in `rs_source2.py` (`RS_LOOKBACK_CALENDAR_DAYS`, `RS_MOMENTUM_WINDOWS`, `RS_BLEND_RS_WEIGHT`);
`rs_matrix_3T.py` imports them, `intraday_rs_3T.py` keeps synced copies (must not import `rs_source2` — it
pulls vnstock into the 15-min tick). EOD↔intraday parity verified bit-exact (0/227). **Crypto
(`rs_matrix_crypto.py`) was deliberately NOT updated** — still 90d / 5/10/20 / 0.30. The two heatmaps now
use different RS definitions; user said leave crypto for now (see §5 open items).

## 2. CI + branch (commits `6392486`, `3f2d0f8`)

The old `update_chart.yml` built a Docker image for the **deleted** Cloud Run jobs and failed on every
push. Replaced with a **validation-only** workflow (`.github/workflows/validate.yml`): `py_compile` all
tracked `.py`, `bash -n` all `.sh`, systemd-unit sanity. No GCP, no Docker, no cost. `infra-cost-protection`
was merged into `master` (reconciling a concurrent upstream removal of the old workflow). Master push runs
`validate.yml` green in ~10s. **CI still does not deploy to the VM** — deploys remain manual.

## 3. Data-sanity + logic-framework audit + fixes (commit `d51f0e9`)

A 4-reviewer adversarial audit (each verified against live data) found the EOD path solid but flagged real
gaps. Fixes, grouped, all deployed + QA-passed + confirmed by a full live VM pipeline run:

**Ingestion** (`eod_batch_downloader.py`, `rs_source2.py`, `run_daily_update.py`)
- `_drop_degenerate_ohlc_rows`: reject `close<=0`, `high<low`, close/open outside `[low,high]`, `volume<0`
  on both SSI and vnstock paths before caching (a halted ticker's `0/1000=0.0` close was surviving `dropna`
  and going false-bearish). 0 false positives on live data; drops 7/7 synthetic bad rows.
- Per-ticker **scale-sanity guard**: reject a fetch whose median close is >100×/<0.01× the prior
  cached/archived median (raw-VND MSN/FMP fallback, a vnstock VNINDEX scale change). Conservative — only
  catches gross ~1000× errors. Keeps the SSI ÷1000 convention.
- Warn on tickers with <200 bars (200-day MA undefined). Naive datetime → ICT.

**RS matrix** (`rs_matrix_3T.py`, `intraday_rs_3T.py`)
- `latest_rs_rating` falls back to a ticker's most-recent rated session (a name halted on the latest day no
  longer sinks its whole heatmap row to the bottom — live effect: CRV/STG rescued).
- NaN `market_cap` coalesced for SORT ONLY (CSV value unchanged); tolerant `calculate_benchmark_return_90d`
  so one bad VNINDEX base bar no longer blanks the whole session column.
- Intraday matches EOD NaN handling exactly (removed `.fillna(0.0)`, same row-gate) and slices
  `history[time < today]` in the momentum/return helpers. Parity stays bit-exact.

**Breadth + publish** (`market_breadth.py`, `intraday_breadth.py`, `deploy/vm/run.sh`)
- **Intraday freshness guard** — the EOD stale-bug's unguarded twin. Fail-closed if the SMA anchor isn't
  the expected previous trading day, and skip on absent/sub-quorum live prices instead of anchoring
  `sma.iloc[-1]` positionally.
- **Drop stale tickers from the breadth cross-section** — any top-100 ticker whose own latest bar
  < `latest_session` is NaN-voided at the latest session only (history untouched), so a T-1 close is never
  `ffill`'d and counted as T. **Live effect: CRV (last bar T-1) was doing exactly this** and is now excluded
  (denominator 99→98).
- Session/settlement banners derive from the data date (`breadth.index[-1]`), not wall-clock; real per-row
  `sample_size` (95–99) instead of hardcoded 100; US half-day off-by-one fix; `read_tickers` logs its source.
- `run.sh`: **transactional publish** — data objects first, `index.html` **last**, `fail=1` on every
  `gsutil cp` failure (was: partial publish reported success). R2 legs stay best-effort (`|| true`).

## 4. VN trading calendar (`vn_trading_calendar.py`, new — part of `d51f0e9`)

Stdlib-only HOSE calendar so the pipeline has a real "is today a trading day / what was the previous
trading day," replacing the weekend-only approximation. **2026 is AUTHORITATIVE** (transcribed from HOSE's
official notice dated 2025-12-09):

- New Year Jan 1–2 · Tết **Feb 16–20** · Hung Kings Apr 27 · Reunification+Labour Apr 30 & May 1 ·
  National Day **Aug 31, Sep 1, Sep 2**. HOSE confirmed **no make-up Saturday trading**, so weekends never
  trade. **2027 is PROVISIONAL** (statutory fixed dates + estimated Tết) until HOSE publishes its 2027
  notice (~Dec 2026). `is_year_authoritative()` flags which.

Wired in two places:
- **Intraday** (`intraday_breadth.py`): the stale-anchor guard now expects the CORRECT prior session after a
  holiday (e.g. Sep 3 → **Aug 28** across the National Day break) instead of over-suppressing; `is_trading_window`
  skips listed holidays.
- **EOD guard** (`market_breadth.py`): distinguishes an expected VN holiday ("publishing prior session, as
  expected") from a source-lag signal ("the VN calendar says IS a trading day … investigate the feed").

**Safety model:** fail-safe. The calendar is only ever used to REJECT a stale anchor or classify a log, so a
wrong/absent year degrades to weekend-only over-suppression, never a bogus publish; data-driven checks back
it. **YEARLY UPDATE:** add the next year's dates from HOSE's notice and mark the year authoritative (a
runtime WARNING fires when running in a non-authoritative year). Verified live on the VM: National Day case
resolves to Aug 28; holiday suppression correct; imports resolve; full pipeline ran clean.

## 5. Billing rework (done by the USER, concurrent 2026-08-16) + a footgun

Confirmed with the user this was their own change. Current verified state (memory `reference_gcp_billing`
updated):
- The two Cloud Functions consolidated into ONE: **`gcp-billing-monitor`** (gen2, asia-southeast1, entry
  `handle_budget_alert`; source `d:\Claude\Devops\cloud_functions\killswitch\main.py`).
- **Killswitch is threshold-based + TOPIC-WIDE, not budget-name-based.** `_LEVELS`: 60/80/90% = Telegram
  notify, **100%/120% = detach**, for ANY budget on the `billing-budget-alerts` topic (gated only by an
  account allowlist). Kill floor raised 90%→**100%** on 2026-08-16.
- Budgets: `vnsafe-bot-safety-1usd` (₫1) **DELETED**; main budget now **"Whole-account cap 230000 VND
  (detach at 100%)"** @ 230,000 VND (was 25,800), wired to the topic. Telegrams ~138k/184k/207k VND, detach
  at 230k — ~24× the ~9,500 VND/mo net run-rate. `c08-demo-budget` (150k) is a different project on its own
  topic — not connected to this killswitch.
- **FOOTGUN (recorded, not fixed):** because the killswitch keys on threshold not identity, wiring any small
  budget (e.g. a $1 tripwire) to that topic would arm a killswitch at that budget's 100%. This is why the
  "$1 Telegram tripwire" idea was dropped. Optional one-line fix: guard `gcp-billing-monitor` to only detach
  the designated cap budget. User's call — not done.

## 6. Cointrading taker_split 30-day prune (deployed to VM, NOT in git)

`sync_state.sh` now prunes `cache/taker_split/*/YYYY-MM-DD.parquet` older than 30 days (pattern
`????-??-??.parquet` — the live `recent_trades.parquet` ring buffers are never matched). Verified live:
597→225 dated parquets, all 95 `recent_trades.parquet` preserved, sync still exit 0, backup tar ~4MB→~2MB.
Cointrading has **no git repo** (user chose to leave it) — the change lives on the VM + the local unversioned
`d:\Claude\Cointrading\deploy\vm\sync_state.sh`. VM backup: `sync_state.sh.pre-prune-20260816.bak`.
(Earlier this session: the sync_state Class-B-runaway fix, `sync_state.sh.pre-tar-20260717.bak`.)

## 7. User decisions this session (2026-08)

- **Pre-breakout gate under option C** → keep momentum-tilted (RS≥90 now selects fresher-momentum names,
  arguably more "about to break out"). No change.
- **Crypto RS param drift** → leave for now (VN=option C, crypto=old params).
- **Cointrading version control** → leave as-is (no git-init).
- **taker_split retention** → 30 days (done, §6).
- **₫1 budget** → delete + rename (found already done by the user's own concurrent rework, §5).

## 8. Universe liquidity cleanup (commit `3678c5b`)

The RS heatmap showed blank cells (e.g. CRV on Aug 18). **Root cause verified: genuine no-trade days** —
illiquid stocks with no price bar on some sessions (both SSI *and* vnstock agree; the new OHLC filter dropped
nothing). Rather than paper over the gaps, the low-quality names were pruned (user's call). Liquidity review
over 60 sessions (coverage = fraction of sessions traded; ADV = median daily value traded, universe median
12.8 bn VND). **Removed 36 of 230** from `rs_fixed_tickers.csv` → **194**:

- **Tier 1 (14, don't trade every session — the blank-makers):** LGC (0% coverage), CRV, STG, HNA, PTI, TMS,
  VCF, BHN, ACG, PDN, DTK, VSH, AST, TDM.
- **Tier 2 (22, trade daily but < 1 bn VND/day turnover):** BAB, CDN, SHP, PGV, VIF, TRA, DSC, DHT, IMP, SAM,
  DHG, IPA, MCM, TNH, BIC, DBD, NTC, HAX, TTF, FIT, BMI, PPC.

**Breadth top-100** (`tickers.csv`): removed the 6 illiquid overlaps (LGC, CRV, VSH, BAB, PGV, DHG) and
backfilled with the 6 largest **liquid** non-top-100 names — GEL, HPA, DSE, HSG, BWE, CEO (all 100% coverage,
ADV ≥ 1 bn). Also pruned the same names from `institutional_universe_3T.csv` (the manual `--sync-universe`
source, a static hand-committed snapshot — nothing auto-regenerates it) so a re-sync can't re-add them.
Verified live: 194×20 heatmap with **0 blank cells**. Rollback `/opt/market-breadth/.rollback-20260820-universe`.

## 9. Automated liquidity screen + read_tickers CSV-first (commit `675bf19`)

So low-quality names **never re-enter / never blank the heatmap** without hand-editing the universe again.

- **read_tickers** (`market_breadth.py`) now reads the versioned `tickers.csv` FIRST (Excel demoted to
  last-resort) — production already used tickers.csv; this makes local runs agree instead of silently using a
  stale desktop Excel.
- **`liquidity_screen.py`** (NEW; pandas/numpy-only **leaf** — must never import `rs_source2`/vnstock so the
  15-min intraday tick stays cheap): `screen_universe()` computes per ticker over the benchmark's last 60
  sessions — **coverage** (traded sessions / sessions-since-listing; close≤0 or NaN/0-volume = MISSING) and
  **median ADV** (close·volume·1000 VND). **Hysteresis** (enter ≥0.98/1 bn, drop only <0.95/0.8 bn) stops
  day-to-day churn; **coldstart** exempts a genuinely NEW listing (< 20 window sessions since first bar) but an
  OLD rarely-trading name — or a totally-absent one — is DROPPED.
- Wiring: `rs_matrix_3T.build_rs_matrix` screens the cohort before building and writes **`rs_screen_members.csv`**
  (generated, **gitignored**); `intraday_rs_3T._load_rs_universe` re-reads that same file (never recomputes)
  for EOD↔intraday parity; `market_breadth.load_fixed_rs_universe` filters to the kept-set so the alignment
  AUDIT / "(N CP)" count / display order expect the screened subset (no false "Missing"); `warn_illiquid_breadth_names`
  **warns but never drops** a top-100 breadth name (dropping one would trip the 0.80-coverage abort).
- **FAIL-SAFE** everywhere: a missing/empty members file → every consumer uses the FULL universe; if the screen
  would keep < 120 (a volume-feed regression) `build_rs_matrix` keeps full AND writes an all-kept membership so
  the consumers coordinate to full. **The screen is a RENDER-TIME filter** — the locked `rs_fixed_tickers.csv`
  is the candidate list, and the screen is now the ongoing guard that supersedes the manual §8 curation.

Since the universe was already curated to 194 liquid names, the screen is a **day-1 no-op** (kept 194, dropped
0, AUDIT SUCCESS — verified live) that then auto-catches future drift. Adversarial QA passed (2 dormant gaps —
coldstart over-exemption, fail-safe coordination — both fixed here). Rollback `.rollback-20260820-screen`.

## 10. Open threads

1. **R2 cutover — the only substantive open item.** Blocked on a Cloudflare account: needs Account ID +
   Access Key ID + Secret, then the `pub-*.r2.dev` URL. `r2_publish.py` + `run.sh`'s `r2pub` leg are in the
   repo, inert until the env vars exist, and NOT yet on the VM (the VM's `run.sh` has the transactional-publish
   version; the R2 leg goes on at cutover). Chosen public URL: r2.dev.
2. **VN calendar 2027** — populate from HOSE's 2027 notice when published (~Dec 2026); a 2-min edit.
3. **Deferred audit items (non-blocking):** crypto RS param drift (VN vs crypto mismatch); a weekend logs as
   "holiday" (cosmetic); the killswitch footgun (§5); crypto benchmark silent-stale; a few Tier-3 items in
   the audit (NaN market_cap leaders skew is fixed; pre_breakout single-session volatility accepted).
4. **No CD to the VM** — master being green ≠ VM updated. Deploys are manual (`scp` + install); recipe in
   `STALE_DASHBOARD_FIX.md` / `CLAUDE.md`. Nothing pages on failure.
