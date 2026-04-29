#!/usr/bin/env python3
"""Reconcile the fresh Institutional 3T scan against the locked RS universe."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from rs_source2 import RS_FIXED_TICKERS_PATH, configure_logging


SCRIPT_DIR = Path(__file__).resolve().parent
INSTITUTIONAL_UNIVERSE_3T_PATH = SCRIPT_DIR / "institutional_universe_3T.csv"
LOGS_DIR = SCRIPT_DIR / "logs"
DRIFT_LATEST_PATH = LOGS_DIR / "universe_drift_latest.txt"
SIGNIFICANT_DRIFT_THRESHOLD = 3

LOGGER = configure_logging("rs_universe_generator")


def load_current_scan() -> pd.DataFrame:
    if not INSTITUTIONAL_UNIVERSE_3T_PATH.exists():
        raise FileNotFoundError(
            f"Current 3T scan not found: {INSTITUTIONAL_UNIVERSE_3T_PATH}"
        )

    current_df = pd.read_csv(INSTITUTIONAL_UNIVERSE_3T_PATH)
    if current_df.empty or "ticker" not in current_df.columns:
        raise ValueError(
            "institutional_universe_3T.csv is empty or missing the 'ticker' column."
        )

    current_df["ticker"] = current_df["ticker"].astype(str).str.upper().str.strip()
    if "industry" in current_df.columns:
        current_df["industry"] = current_df["industry"].fillna("Chưa phân ngành").astype(str).str.strip()
    else:
        current_df["industry"] = "Chưa phân ngành"

    current_df = current_df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    current_df["market_cap"] = pd.to_numeric(current_df.get("market_cap"), errors="coerce")
    return current_df


def load_locked_universe() -> pd.DataFrame:
    if not RS_FIXED_TICKERS_PATH.exists():
        return pd.DataFrame(columns=["ticker", "industry", "market_cap"])

    locked_df = pd.read_csv(RS_FIXED_TICKERS_PATH)
    if locked_df.empty or "ticker" not in locked_df.columns:
        raise ValueError("rs_fixed_tickers.csv is empty or missing the 'ticker' column.")

    locked_df["ticker"] = locked_df["ticker"].astype(str).str.upper().str.strip()
    if "industry" in locked_df.columns:
        locked_df["industry"] = locked_df["industry"].fillna("Chưa phân ngành").astype(str).str.strip()
    else:
        locked_df["industry"] = "Chưa phân ngành"

    locked_df = locked_df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    locked_df["market_cap"] = pd.to_numeric(locked_df.get("market_cap"), errors="coerce")
    return locked_df


def build_drift_summary(current_df: pd.DataFrame, locked_df: pd.DataFrame) -> dict:
    current_map = current_df.set_index("ticker").to_dict("index")
    locked_map = locked_df.set_index("ticker").to_dict("index") if not locked_df.empty else {}

    current_tickers = set(current_map.keys())
    locked_tickers = set(locked_map.keys())

    additions = []
    for ticker in sorted(current_tickers - locked_tickers):
        row = current_map[ticker]
        additions.append(
            {
                "ticker": ticker,
                "industry": row.get("industry") or "Chưa phân ngành",
                "market_cap": row.get("market_cap"),
            }
        )

    removals = []
    for ticker in sorted(locked_tickers - current_tickers):
        row = locked_map.get(ticker, {})
        removals.append(
            {
                "ticker": ticker,
                "industry": row.get("industry") or "Chưa phân ngành",
                "market_cap": row.get("market_cap"),
            }
        )

    return {
        "scan_date": date.today().isoformat(),
        "current_count": len(current_tickers),
        "locked_count": len(locked_tickers),
        "additions": additions,
        "removals": removals,
        "total_changes": len(additions) + len(removals),
        "is_significant": (len(additions) + len(removals)) >= SIGNIFICANT_DRIFT_THRESHOLD,
    }


def format_drift_report(summary: dict, sync_enabled: bool) -> str:
    lines = [
        f"Universe Drift Report | {summary['scan_date']}",
        f"Current 3T scan size: {summary['current_count']}",
        f"Locked universe size: {summary['locked_count']}",
        f"Additions: {len(summary['additions'])}",
        f"Removals: {len(summary['removals'])}",
        f"Total changes: {summary['total_changes']}",
        f"Significant drift: {'YES' if summary['is_significant'] else 'NO'}",
        f"Sync mode: {'ENABLED' if sync_enabled else 'REPORT ONLY'}",
        "",
        "Drift Summary",
        "-------------",
    ]

    if not summary["additions"] and not summary["removals"]:
        lines.append("No drift detected between the fresh 3T scan and the locked universe.")
        return "\n".join(lines) + "\n"

    if summary["additions"]:
        lines.append("Additions:")
        for item in summary["additions"]:
            lines.append(
                f"[UNIVERSE ADDITION] New institutional candidate: "
                f"{item['ticker']} ({item['industry']})"
            )

    if summary["removals"]:
        lines.append("Removals:")
        for item in summary["removals"]:
            lines.append(
                f"[UNIVERSE REMOVAL] Ticker dropped below 3T floor: {item['ticker']}"
            )

    return "\n".join(lines) + "\n"


def write_drift_report(summary: dict, sync_enabled: bool) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    report_text = format_drift_report(summary, sync_enabled)
    dated_path = LOGS_DIR / f"universe_drift_{summary['scan_date']}.txt"
    dated_path.write_text(report_text, encoding="utf-8")
    DRIFT_LATEST_PATH.write_text(report_text, encoding="utf-8")
    return dated_path


def sync_locked_universe(current_df: pd.DataFrame) -> None:
    synced_df = current_df.copy()
    synced_df["locked_at"] = date.today().isoformat()
    synced_df["lock_rule"] = "Synced from institutional_universe_3T.csv via --sync-universe"
    synced_df.to_csv(RS_FIXED_TICKERS_PATH, index=False, encoding="utf-8-sig")
    LOGGER.info(
        "Locked universe updated: %s (%s tickers)",
        RS_FIXED_TICKERS_PATH,
        len(synced_df),
    )


def log_summary(summary: dict) -> None:
    LOGGER.info("Drift Summary")
    LOGGER.info(
        "Current 3T scan=%s | locked universe=%s | additions=%s | removals=%s | total changes=%s",
        summary["current_count"],
        summary["locked_count"],
        len(summary["additions"]),
        len(summary["removals"]),
        summary["total_changes"],
    )
    for item in summary["additions"]:
        LOGGER.info(
            "[UNIVERSE ADDITION] New institutional candidate: %s (%s)",
            item["ticker"],
            item["industry"],
        )
    for item in summary["removals"]:
        LOGGER.info(
            "[UNIVERSE REMOVAL] Ticker dropped below 3T floor: %s",
            item["ticker"],
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sync-universe",
        action="store_true",
        help="Replace rs_fixed_tickers.csv with the latest institutional_universe_3T.csv snapshot.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Ignored. Accepted for pipeline compatibility.",
    )
    args = parser.parse_args()

    LOGGER.info("Starting institutional universe reconciliation")
    current_df = load_current_scan()
    locked_df = load_locked_universe()
    summary = build_drift_summary(current_df, locked_df)
    log_summary(summary)

    report_path = write_drift_report(summary, sync_enabled=args.sync_universe)
    LOGGER.info("Universe drift report saved: %s", report_path)

    if args.sync_universe:
        sync_locked_universe(current_df)
    else:
        LOGGER.info("Locked universe preserved. Run with --sync-universe to accept the latest 3T scan.")


if __name__ == "__main__":
    main()
