#!/usr/bin/env python3
"""Vietnam (HOSE/HNX) trading-day calendar — stdlib only.

Purpose: give the pipeline a real answer to "is today a trading day?" and "what
was the previous trading day?" instead of a weekend-only approximation. The VN
market trades Mon-Fri minus public holidays; HOSE does NOT run make-up Saturday
sessions (the 2026 notice explicitly says so for the rescheduled Jan-10 / Aug-22
Saturdays), so weekends are always non-trading and no Saturday ever trades.

Deliberately dependency-free (only datetime): `intraday_breadth.py` imports this
on the every-15-min hot path and must not pull anything heavy.

SOURCE OF TRUTH: HOSE's official "Notice of trading holiday schedule" PDF, issued
~December of the prior year. 2026 below is transcribed from the notice dated
2025-12-09 (holidays.hsx.vn). UPDATE YEARLY: when HOSE publishes the next year's
notice, add its dates and mark the year authoritative.

SAFETY MODEL: every consumer treats a wrong/absent calendar as fail-safe. The
"previous trading day" is only ever used to REJECT a stale intraday anchor, so a
wrong date can at worst over-suppress an intraday tick (never publish stale). And
"is today trading" is backed by a data-driven check (the live feed must actually
return today's data) — the calendar is a hint there, not the sole gate. So a year
that is only provisional (or missing) degrades to safe over-suppression, never to
a bogus publish.
"""
from __future__ import annotations

from datetime import date, timedelta

# Non-trading WEEKDAYS only (weekends are handled separately). Transcribe each
# year from HOSE's official notice. Do NOT include the rescheduled make-up
# Saturdays — HOSE confirmed it does not trade them, and weekends are already off.
_HOLIDAYS: dict[int, frozenset[date]] = {
    # 2026 — AUTHORITATIVE. HOSE notice dated 2025-12-09.
    2026: frozenset({
        date(2026, 1, 1), date(2026, 1, 2),                 # New Year
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20),               # Tet (Lunar New Year)
        date(2026, 4, 27),                                  # Hung Kings' Day
        date(2026, 4, 30), date(2026, 5, 1),                # Reunification + Labour
        date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2),  # National Day
    }),
    # 2027 — PROVISIONAL. HOSE's 2027 notice is not published yet (issued ~Dec
    # 2026). Statutory fixed-date holidays are certain; the Tet range is estimated
    # from the lunar calendar (mung 1 = 2027-02-06) and the 2026 closure pattern
    # (lunar Dec 29 -> ~4th day). CONFIRM against HOSE's 2027 notice when out;
    # until then this only tightens the fail-safe over-suppression window.
    2027: frozenset({
        date(2027, 1, 1),                                   # New Year
        date(2027, 2, 4), date(2027, 2, 5),                 # Tet — PROVISIONAL
        date(2027, 2, 8), date(2027, 2, 9), date(2027, 2, 10),  # Tet — PROVISIONAL
        date(2027, 4, 30),                                  # Reunification
        # 2027-05-01 (Labour) falls on a Saturday — already non-trading.
        date(2027, 9, 2),                                   # National Day
        # Hung Kings' Day 2027 (10th day, 3rd lunar month) — PROVISIONAL, omitted
        # pending the official notice rather than guessed.
    }),
}

# Years transcribed directly from a HOSE notice (vs estimated). Consumers may log
# a warning when operating in a non-authoritative year to prompt a yearly update.
_AUTHORITATIVE_YEARS: frozenset[int] = frozenset({2026})


def is_year_authoritative(year: int) -> bool:
    """True if `year`'s holidays come from HOSE's official notice (not estimated)."""
    return year in _AUTHORITATIVE_YEARS


def is_holiday(d: date) -> bool:
    """True if `d` is a listed VN market public holiday (ignores weekends)."""
    return d in _HOLIDAYS.get(d.year, frozenset())


def is_trading_day(d: date) -> bool:
    """True if the VN market trades on `d` (a weekday that is not a holiday).

    For a year with no calendar data, this is weekday-only (holidays unknown) —
    callers should pair it with the data-driven freshness check.
    """
    return d.weekday() < 5 and not is_holiday(d)


def previous_trading_day(d: date) -> date:
    """Most recent trading day strictly before `d`.

    Bounded scan (30 days) covers the longest VN closure (Tet, ~9 days) with
    margin; if it somehow finds none it returns the 30-day-back date rather than
    looping forever — a value that will simply fail the anchor check (fail-safe).
    """
    probe = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    return probe


def next_trading_day(d: date) -> date:
    """Earliest trading day strictly after `d` (bounded scan, fail-safe)."""
    probe = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(probe):
            return probe
        probe += timedelta(days=1)
    return probe
