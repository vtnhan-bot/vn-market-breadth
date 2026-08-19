#!/usr/bin/env python3
"""Render-time liquidity screen for the RS universe.

Keeps low-quality (illiquid) names out of the RS heatmap automatically, so a
stock that stops trading every session (blank cells) or drops to negligible
turnover is excluded from the ranking WITHOUT hand-editing rs_fixed_tickers.csv.
Computed once per EOD build in rs_matrix_3T; the kept-set is persisted to
`rs_screen_members.csv` and re-read by intraday_rs_3T so the live HH:MM heatmap
and the settled 15:15 heatmap rank over the IDENTICAL membership (parity).

Deliberately a LEAF module: imports only pandas/numpy. It must NOT import
rs_source2 (which pulls vnstock at load) — this is the same reason intraday_rs_3T
duplicates the RS constants instead of importing them.

Screen (over the benchmark's last SCREEN_WINDOW sessions):
  coverage    = traded sessions in window / SCREEN_WINDOW   (a no-trade or
                zero/NaN-volume session counts as MISSING, never as ADV=0)
  median_adv  = median of close(thousand-VND) * volume(shares) * 1000  (VND/day)

coverage is measured over the sessions the name COULD have traded (window
sessions on or after its first-ever bar), so a recently-listed name is not
penalised for the window sessions that predate its listing.

Hysteresis (needs memory of last run's kept-set to avoid day-to-day churn):
  - a name NOT previously kept (or first run): strict ENTRY band — keep only if
    coverage >= ENTRY_MIN_COVERAGE AND median_adv >= ENTRY_MIN_ADV_VND.
  - a name previously kept: looser KEEP band — drop only if
    coverage < KEEP_MIN_COVERAGE OR median_adv < KEEP_MIN_ADV_VND.
  - a name listed for fewer than SCREEN_MIN_BARS window sessions: cold-start
    pass-through (kept, exempt) so a genuinely NEW listing is never locked out
    while warming up. An OLD name that merely trades rarely (listed before the
    window but with few prints) is NOT exempt — it is screened and dropped; a
    totally-absent name (no bars at all) is dropped, never coldstart.

FAIL-SAFE: the caller falls back to the FULL universe when the members file is
missing/empty or the screen would keep fewer than a floor — showing an illiquid
name beats blanking the heatmap or tripping the downstream 95-ticker abort.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SCREEN_WINDOW = 60            # benchmark sessions the screen looks back over
SCREEN_MIN_BARS = 20         # < this many bars in the window -> cold-start (keep)
ENTRY_MIN_COVERAGE = 0.98    # strict band for a name entering / on first run
ENTRY_MIN_ADV_VND = 1_000_000_000
KEEP_MIN_COVERAGE = 0.95     # looser band once a name is already kept (hysteresis)
KEEP_MIN_ADV_VND = 800_000_000
# If the screen would keep fewer than this, something is wrong (e.g. a volume
# feed regression NaN-ing everything) -> the caller must fall back to full.
FAILSAFE_MIN_KEPT = 120
INDEX_TICKER = "VNINDEX"

MEMBERS_COLUMNS = ["ticker", "coverage", "median_adv_vnd", "nbars", "status", "screened_at"]


def screen_universe(
    combined_df: pd.DataFrame,
    tickers,
    screen_sessions,
    prior_members=None,
    index_ticker: str = INDEX_TICKER,
):
    """Screen `tickers` for liquidity over `screen_sessions`.

    Returns (kept: list[str], dropped: list[str], stats: DataFrame). Pure — no
    I/O, no fetch. `combined_df` must carry ticker/time(date)/close/volume.
    `prior_members` is the set kept last run (drives the hysteresis band).
    """
    prior = set(prior_members or [])
    sset = set(screen_sessions)
    sorted_sessions = sorted(sset)
    nwin = len(sset) or 1

    all_df = combined_df[["ticker", "time", "close", "volume"]].copy()
    all_df["ticker"] = all_df["ticker"].astype(str).str.upper().str.strip()
    # first-ever bar per ticker (unwindowed) -> distinguishes a NEW listing from an
    # OLD name that trades rarely, and bounds coverage to the listed period.
    first_bar = all_df.groupby("ticker")["time"].min().to_dict()

    df = all_df[all_df["time"].isin(sset)].copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    # a "traded" session needs a real print: positive close AND positive volume.
    df = df[(df["close"] > 0) & (df["volume"].fillna(0) > 0)]
    grp = df.groupby("ticker")

    kept: list[str] = []
    dropped: list[str] = []
    rows: list[dict] = []
    for t in tickers:
        t = str(t).upper().strip()
        if not t or t == index_ticker:
            if t:
                kept.append(t)
            continue
        sub = grp.get_group(t) if t in grp.groups else df.iloc[0:0]
        nbars = int(sub["time"].nunique())
        fb = first_bar.get(t)
        # sessions the name COULD have traded: window sessions on/after its first
        # bar. Absent (fb is None) -> full window -> coverage 0 -> dropped.
        n_possible = sum(1 for s in sorted_sessions if s >= fb) if fb is not None else nwin
        n_possible = n_possible or nwin
        coverage = nbars / n_possible
        median_adv = float((sub["close"] * sub["volume"]).median()) * 1000.0 if nbars else 0.0

        if n_possible < SCREEN_MIN_BARS:
            # listed for fewer than SCREEN_MIN_BARS sessions -> genuinely new, too
            # little history to judge. (Absent/old names have n_possible == nwin.)
            status, keep = "coldstart", True
        elif t in prior:
            keep = not (coverage < KEEP_MIN_COVERAGE or median_adv < KEEP_MIN_ADV_VND)
            status = "kept" if keep else "dropped"
        else:
            keep = coverage >= ENTRY_MIN_COVERAGE and median_adv >= ENTRY_MIN_ADV_VND
            status = "kept" if keep else "dropped"

        (kept if keep else dropped).append(t)
        rows.append({
            "ticker": t, "coverage": round(coverage, 4),
            "median_adv_vnd": round(median_adv, 0), "nbars": nbars, "status": status,
        })

    stats = pd.DataFrame(rows, columns=["ticker", "coverage", "median_adv_vnd", "nbars", "status"])
    return kept, dropped, stats


def write_members(path, stats: pd.DataFrame, screened_at: str) -> None:
    """Persist the screen result (all tickers with their status) for intraday + audit."""
    out = stats.copy()
    out["screened_at"] = screened_at
    out.reindex(columns=MEMBERS_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")


def read_kept_members(path) -> set | None:
    """Return the set of tickers whose status is kept/coldstart, or None if the
    file is missing/unreadable/empty (caller then FAILS SAFE to the full universe).
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
        if df.empty or "ticker" not in df.columns or "status" not in df.columns:
            return None
        kept = set(
            df.loc[df["status"].isin(["kept", "coldstart"]), "ticker"].astype(str).str.upper().str.strip()
        )
        return kept or None
    except Exception:
        return None
