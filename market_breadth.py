#!/usr/bin/env python3
"""
Vietnam Market Breadth - "Cơ hội" Chart Generator
Formula: mbzN = % of top-100 stocks (by market cap, HOSE+HNX) with Close > N-day SMA
Periods: 3, 5, 10, 20, 50, 200 sessions
"""

import os
import sys
import json
import time
import webbrowser
import warnings
import argparse
import logging
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np

# ─── CONFIG ──────────────────────────────────────────────────────────────────
EXCEL_PATH = r"C:\Users\DELL\Desktop\vietnam_top100_marketcap_hose_hnx_best_effort.xlsx"
SCRIPT_DIR = Path(__file__).parent
AUDIT_DIR  = SCRIPT_DIR / "audit_logs"
OUTPUT_HTML = SCRIPT_DIR / "market_breadth.html"
DATA_DIR = SCRIPT_DIR / "data"
ICT = ZoneInfo("Asia/Ho_Chi_Minh")

MA_PERIODS      = [3, 5, 10, 20, 50, 200]
SESSIONS_SHOW   = 50
US_INDEX_SESSIONS = 100  # CBOE VIX & Nasdaq Composite display window
IS_CI           = bool(os.environ.get("GITHUB_ACTIONS"))
CACHE_HOURS     = 4     # Re-fetch if cache older than N hours
AUDIT_EXPORT_FORMAT = "csv"
MIN_SUCCESSFUL_TICKERS = 95
INDEX_TICKER = "VNINDEX"
INSTITUTIONAL_UNIVERSE_3T_PATH = SCRIPT_DIR / "institutional_universe_3T.csv"
RS_FIXED_TICKERS_PATH = SCRIPT_DIR / "rs_fixed_tickers.csv"
RS_MATRIX_3T_PATH = SCRIPT_DIR / "rs_matrix_3T.csv"
UNIVERSE_DRIFT_LATEST_PATH = SCRIPT_DIR / "logs" / "universe_drift_latest.txt"
SIGNIFICANT_DRIFT_THRESHOLD = 3

MA_COLORS = {
    3:   "#00BCD4",   # cyan
    5:   "#FFA726",   # orange
    10:  "#43A047",   # green
    20:  "#9C27B0",   # purple
    50:  "#000000",   # black
    200: "#E53935",   # red
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def setup_logger():
    logger = logging.getLogger("market_breadth")
    if logger.handlers:
        return logger

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger

LOGGER = setup_logger()

def log(msg):
    LOGGER.info(str(msg))

def ma_key(p):
    return f"mbz{p:02d}" if p < 100 else f"mbz{p}"

def get_today_combined_dataset_path():
    today_str = datetime.now(ICT).date().isoformat()
    return DATA_DIR / today_str / "combined_dataset.csv"

def verify_fresh_eod_dataset():
    combined_path = get_today_combined_dataset_path()
    if not combined_path.exists():
        raise RuntimeError("CRITICAL: EOD Data Not Fresh. Aborting HTML Update.")

    modified_at = datetime.fromtimestamp(combined_path.stat().st_mtime, ICT)
    now_ict = datetime.now(ICT)
    freshness_cutoff = now_ict.replace(hour=15, minute=30, second=0, microsecond=0)

    if modified_at.date() != now_ict.date() or modified_at < freshness_cutoff:
        raise RuntimeError("CRITICAL: EOD Data Not Fresh. Aborting HTML Update.")

    return combined_path, modified_at

def get_last_three_combined_tickers(combined_path):
    try:
        combined_df = pd.read_csv(combined_path)
        if "ticker" not in combined_df.columns or combined_df.empty:
            return []
        return combined_df["ticker"].dropna().astype(str).tail(3).tolist()
    except Exception:
        return []

def load_institutional_universe():
    if not INSTITUTIONAL_UNIVERSE_3T_PATH.exists():
        return pd.DataFrame(columns=["ticker", "company_name", "market_cap", "industry", "universe_order"])

    universe_df = pd.read_csv(INSTITUTIONAL_UNIVERSE_3T_PATH)
    if universe_df.empty or "ticker" not in universe_df.columns:
        return pd.DataFrame(columns=["ticker", "company_name", "market_cap", "industry", "universe_order"])

    universe_df["ticker"] = universe_df["ticker"].astype(str).str.upper().str.strip()
    universe_df["market_cap"] = pd.to_numeric(universe_df.get("market_cap"), errors="coerce")
    universe_df = universe_df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    universe_df["universe_order"] = np.arange(1, len(universe_df) + 1)
    return universe_df


def load_fixed_rs_universe():
    if not RS_FIXED_TICKERS_PATH.exists():
        return pd.DataFrame(columns=["ticker", "company_name", "market_cap", "industry", "universe_order"])

    fixed_df = pd.read_csv(RS_FIXED_TICKERS_PATH)
    if fixed_df.empty or "ticker" not in fixed_df.columns:
        return pd.DataFrame(columns=["ticker", "company_name", "market_cap", "industry", "universe_order"])

    fixed_df["ticker"] = fixed_df["ticker"].astype(str).str.upper().str.strip()
    fixed_df["market_cap"] = pd.to_numeric(fixed_df.get("market_cap"), errors="coerce")
    fixed_df = fixed_df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    fixed_df["universe_order"] = np.arange(1, len(fixed_df) + 1)
    return fixed_df


def load_rs_matrix_payload():
    fixed_df = load_fixed_rs_universe()
    expected_ticker_count = len(fixed_df)

    if not RS_MATRIX_3T_PATH.exists():
        log("WARNING: rs_matrix_3T.csv missing. RS heatmap will be omitted.")
        return None

    rs_df = pd.read_csv(RS_MATRIX_3T_PATH)
    if rs_df.empty:
        log("WARNING: rs_matrix_3T.csv is empty. RS heatmap will be omitted.")
        return None

    required_columns = {"ticker", "session_date", "rs_rating", "daily_change_pct"}
    missing_columns = required_columns - set(rs_df.columns)
    if missing_columns:
        log(
            "WARNING: rs_matrix_3T.csv is missing required columns: "
            f"{', '.join(sorted(missing_columns))}. RS heatmap will be omitted."
        )
        return None

    rs_df["ticker"] = rs_df["ticker"].astype(str).str.upper()
    rs_df["session_date"] = pd.to_datetime(rs_df["session_date"], errors="coerce")
    rs_df = rs_df.dropna(subset=["ticker", "session_date"])
    if rs_df.empty:
        return None

    rs_df["rs_rating"] = pd.to_numeric(rs_df["rs_rating"], errors="coerce")
    rs_df["daily_change_pct"] = pd.to_numeric(rs_df["daily_change_pct"], errors="coerce")
    if "weighted_momentum_score" in rs_df.columns:
        rs_df["weighted_momentum_score"] = pd.to_numeric(
            rs_df["weighted_momentum_score"], errors="coerce"
        )
    else:
        log("WARNING: rs_matrix_3T.csv has no weighted_momentum_score; falling back to daily_change_pct.")
        rs_df["weighted_momentum_score"] = pd.to_numeric(
            rs_df.get("daily_change_pct"), errors="coerce"
        )
    if "weighted_momentum_rating" in rs_df.columns:
        rs_df["weighted_momentum_rating"] = pd.to_numeric(
            rs_df["weighted_momentum_rating"], errors="coerce"
        )
    else:
        log("WARNING: rs_matrix_3T.csv has no weighted_momentum_rating; ranking weighted_momentum_score on load.")
        rs_df["weighted_momentum_rating"] = (
            ((rs_df.groupby("session_date")["weighted_momentum_score"].rank(pct=True) * 98) + 1)
            .round()
            .clip(1, 99)
        )
    if "latest_rs_rating" in rs_df.columns:
        rs_df["latest_rs_rating"] = pd.to_numeric(rs_df["latest_rs_rating"], errors="coerce")

    session_dates = list(reversed(sorted(rs_df["session_date"].dropna().unique())[-20:]))
    if not session_dates:
        return None
    session_labels = [pd.Timestamp(d).strftime("%d-%m") for d in session_dates]

    latest_date = max(session_dates)
    if "latest_rs_rating" not in rs_df.columns:
        latest_scores = (
            rs_df[rs_df["session_date"] == latest_date][["ticker", "rs_rating"]]
            .rename(columns={"rs_rating": "latest_rs_rating"})
        )
        rs_df = rs_df.merge(latest_scores, on="ticker", how="left")

    if not fixed_df.empty:
        metadata_columns = [
            column
            for column in ["ticker", "company_name", "industry", "market_cap", "universe_order"]
            if column in fixed_df.columns
        ]
        rs_df = rs_df.merge(
            fixed_df[metadata_columns],
            on="ticker",
            how="left",
            suffixes=("", "_universe"),
        )
        if "market_cap_universe" in rs_df.columns:
            rs_df["market_cap"] = rs_df["market_cap"].fillna(rs_df["market_cap_universe"])
            rs_df = rs_df.drop(columns=["market_cap_universe"])
        if "company_name_universe" in rs_df.columns:
            rs_df["company_name"] = rs_df.get("company_name", rs_df["company_name_universe"])
            rs_df = rs_df.drop(columns=["company_name_universe"])
        if "industry_universe" in rs_df.columns:
            rs_df["industry"] = rs_df.get("industry", rs_df["industry_universe"])
            rs_df = rs_df.drop(columns=["industry_universe"])
        if "universe_order_universe" in rs_df.columns:
            rs_df["universe_order"] = rs_df.get("universe_order", rs_df["universe_order_universe"])
            rs_df = rs_df.drop(columns=["universe_order_universe"])

    rs_df["market_cap"] = pd.to_numeric(rs_df.get("market_cap"), errors="coerce")
    rs_df["universe_order"] = pd.to_numeric(rs_df.get("universe_order"), errors="coerce").fillna(9999)

    fixed_tickers = set(fixed_df["ticker"].tolist()) if not fixed_df.empty else set()
    matrix_tickers = set(rs_df["ticker"].dropna().astype(str).str.upper().tolist())
    missing_from_matrix = sorted(fixed_tickers - matrix_tickers)
    extra_in_matrix = sorted(matrix_tickers - fixed_tickers)
    confirmed_count = len(fixed_tickers & matrix_tickers)
    audit_total = len(fixed_tickers)

    if audit_total:
        log(
            f"[AUDIT] RS Universe Verification: {confirmed_count}/{audit_total} "
            "tickers from rs_fixed_tickers.csv confirmed in final matrix."
        )
        if not missing_from_matrix and not extra_in_matrix:
            log("[AUDIT] SUCCESS: RS Table is perfectly aligned with fixed universe.")
        else:
            if missing_from_matrix:
                log(
                    "[AUDIT] Missing from final matrix: "
                    f"{', '.join(missing_from_matrix)}"
                )
            if extra_in_matrix:
                log(
                    "[AUDIT] Present in matrix but outside rs_fixed_tickers.csv: "
                    f"{', '.join(extra_in_matrix)}"
                )

    if not fixed_df.empty:
        latest_rank_map = (
            rs_df[["ticker", "latest_rs_rating"]]
            .drop_duplicates(subset=["ticker"], keep="last")
            .set_index("ticker")["latest_rs_rating"]
            .to_dict()
        )
        ordered_universe = fixed_df.copy()
        ordered_universe["latest_rs_rating"] = ordered_universe["ticker"].map(latest_rank_map)
        ticker_order = (
            ordered_universe.sort_values(
                ["latest_rs_rating", "market_cap", "universe_order", "ticker"],
                ascending=[False, False, True, True],
                na_position="last",
            )["ticker"].tolist()
        )
    else:
        ticker_order = (
            rs_df[["ticker", "latest_rs_rating", "universe_order", "market_cap"]]
            .drop_duplicates()
            .sort_values(
                ["latest_rs_rating", "market_cap", "universe_order", "ticker"],
                ascending=[False, False, True, True],
            )
            ["ticker"]
            .tolist()
        )

    rows = []
    for ticker in ticker_order:
        ticker_slice = rs_df[rs_df["ticker"] == ticker].copy()
        ticker_slice = ticker_slice.set_index("session_date")
        cells = []
        for session_date in session_dates:
            if session_date in ticker_slice.index:
                row = ticker_slice.loc[session_date]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                rs_rating = int(row["rs_rating"]) if pd.notna(row["rs_rating"]) else None
                momentum_rating = (
                    int(row["weighted_momentum_rating"])
                    if pd.notna(row["weighted_momentum_rating"])
                    else None
                )
                momentum_score = (
                    float(row["weighted_momentum_score"])
                    if pd.notna(row["weighted_momentum_score"])
                    else None
                )
                daily_change = (
                    float(row["daily_change_pct"])
                    if pd.notna(row["daily_change_pct"])
                    else None
                )
            else:
                rs_rating = None
                momentum_rating = None
                momentum_score = None
                daily_change = None
            cells.append(
                {
                    "rs_rating": rs_rating,
                    "weighted_momentum_rating": momentum_rating,
                    "weighted_momentum_score": (
                        round(momentum_score, 2) if momentum_score is not None else None
                    ),
                    "daily_change_pct": round(daily_change, 2) if daily_change is not None else None,
                }
            )
        rows.append({"ticker": ticker, "cells": cells})

    visible_ticker_count = len(rows)
    matrix_ticker_count = rs_df["ticker"].nunique()

    return {
        "dates": session_labels,
        "rows": rows,
        "row_count": visible_ticker_count,
        "unique_ticker_count": visible_ticker_count,
        "matrix_ticker_count": matrix_ticker_count,
        "expected_ticker_count": expected_ticker_count,
        "is_complete": (
            visible_ticker_count == expected_ticker_count and not missing_from_matrix and not extra_in_matrix
        ) if expected_ticker_count else True,
        "audit_confirmed_count": confirmed_count,
        "audit_total_count": audit_total,
        "missing_from_matrix": missing_from_matrix,
        "extra_in_matrix": extra_in_matrix,
        "source_label": f"Nguồn: rs_fixed_tickers.csv ({expected_ticker_count or visible_ticker_count} CP)",
        "source_label_is_ok": not missing_from_matrix and not extra_in_matrix,
        "source_label_tooltip": "; ".join(
            ([f"Thiếu: {', '.join(missing_from_matrix)}"] if missing_from_matrix else [])
            + ([f"Dư trong matrix: {', '.join(extra_in_matrix)}"] if extra_in_matrix else [])
        ),
        "footer_text": (
            f"Vũ trụ: rs_fixed_tickers.csv | Quy mô: "
            f"{expected_ticker_count or visible_ticker_count} cổ phiếu"
        ),
    }


def load_universe_drift_payload():
    if not UNIVERSE_DRIFT_LATEST_PATH.exists():
        return None

    try:
        drift_text = UNIVERSE_DRIFT_LATEST_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        log(f"WARNING: Unable to read universe drift report: {exc}")
        return None

    additions = []
    removals = []
    scan_date = None
    for raw_line in drift_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Universe Drift Report | "):
            scan_date = line.split("|", 1)[1].strip()
        elif line.startswith("[UNIVERSE ADDITION] "):
            additions.append(line)
        elif line.startswith("[UNIVERSE REMOVAL] "):
            removals.append(line)

    total_changes = len(additions) + len(removals)
    if total_changes == 0:
        return None

    return {
        "scan_date": scan_date,
        "additions": additions,
        "removals": removals,
        "total_changes": total_changes,
        "is_significant": total_changes >= SIGNIFICANT_DRIFT_THRESHOLD,
    }

def load_price_data_from_combined_dataset(combined_path):
    combined_df = pd.read_csv(combined_path)
    if combined_df.empty:
        raise RuntimeError("CRITICAL: EOD Data Not Fresh. Aborting HTML Update.")

    required_columns = {"time", "ticker", "close"}
    missing_columns = required_columns - set(combined_df.columns)
    if missing_columns:
        raise ValueError(
            f"Combined dataset is missing required columns: {', '.join(sorted(missing_columns))}"
        )

    combined_df["time"] = pd.to_datetime(combined_df["time"]).dt.date
    combined_df["ticker"] = combined_df["ticker"].astype(str).str.strip()
    combined_df["close"] = pd.to_numeric(combined_df["close"], errors="coerce")

    for column in ("open", "high", "low", "volume"):
        if column in combined_df.columns:
            combined_df[column] = pd.to_numeric(combined_df[column], errors="coerce")
        else:
            combined_df[column] = np.nan

    if "source" not in combined_df.columns:
        combined_df["source"] = "unknown"
    else:
        combined_df["source"] = combined_df["source"].fillna("unknown").astype(str)

    combined_df = combined_df.dropna(subset=["time", "ticker", "close"])
    combined_df = combined_df.sort_values(["ticker", "time"]).reset_index(drop=True)

    vnindex_df = combined_df[combined_df["ticker"] == INDEX_TICKER].copy()
    breadth_df = combined_df[combined_df["ticker"] != INDEX_TICKER].copy()

    price_data = {}
    for ticker, ticker_df in breadth_df.groupby("ticker", sort=True):
        normalized = ticker_df.copy()
        normalized = normalized.drop_duplicates("time", keep="last").reset_index(drop=True)
        normalized["change_pct"] = normalized["close"].pct_change().mul(100).round(4)
        if len(normalized) >= 10:
            price_data[ticker] = normalized

    provider_counts = (
        breadth_df[["ticker", "source"]]
        .drop_duplicates()
        ["source"]
        .value_counts()
        .sort_index()
    )
    provider_label = ", ".join(
        f"{source}: {count}" for source, count in provider_counts.items()
    ) if not provider_counts.empty else "unknown"

    if not vnindex_df.empty:
        vnindex_df = vnindex_df.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)

    return combined_df, price_data, provider_label, vnindex_df


def _load_us_index_data(symbol, label, sessions_show, period="1y"):
    """Generic OHLCV loader for a Yahoo Finance index symbol (e.g. ^VIX, ^IXIC)."""
    try:
        import yfinance as yf

        df = yf.download(
            symbol,
            period=period,
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
    except Exception as exc:
        log(f"WARNING: Unable to fetch {label} ({symbol}) data: {exc}")
        return pd.DataFrame()

    if df is None or df.empty:
        log(f"WARNING: {label} ({symbol}) data fetch returned no rows.")
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            str(column[0]).lower().replace(" ", "_")
            for column in df.columns.to_flat_index()
        ]
    else:
        df.columns = [str(column).lower().replace(" ", "_") for column in df.columns]

    df = df.reset_index().rename(columns={"Date": "time", "date": "time"})
    required_columns = {"time", "open", "high", "low", "close"}
    missing_columns = required_columns - set(df.columns)
    if missing_columns:
        log(
            f"WARNING: {label} ({symbol}) data missing columns: "
            f"{', '.join(sorted(missing_columns))}"
        )
        return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 0

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["time", "open", "high", "low", "close"])
    return df.sort_values("time").tail(sessions_show).reset_index(drop=True)


def load_us_vix_index_data(sessions_show=US_INDEX_SESSIONS):
    return _load_us_index_data("^VIX", "CBOE VIX", sessions_show, period="1y")


def load_us_nasdaq_index_data(sessions_show=US_INDEX_SESSIONS):
    return _load_us_index_data("^IXIC", "Nasdaq Composite", sessions_show, period="1y")

# ─── STEP 1: Read tickers (Excel if available, else CSV fallback) ─────────────
def read_tickers():
    csv_path = SCRIPT_DIR / "tickers.csv"
    try:
        df = pd.read_excel(EXCEL_PATH, header=2)
        tickers = df["Ticker"].dropna().astype(str).str.strip().tolist()
        tickers = [t for t in tickers if t and t != "nan"][:100]
        log(f"Loaded {len(tickers)} tickers from Excel")
        return tickers
    except Exception:
        pass
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        tickers = df["Ticker"].dropna().astype(str).str.strip().tolist()
        tickers = [t for t in tickers if t and t != "nan"][:100]
        log(f"Loaded {len(tickers)} tickers from tickers.csv (Excel not found)")
        return tickers
    raise FileNotFoundError("No ticker source found. Provide Excel or tickers.csv.")

# ─── STEP 2: Fetch price data with caching ───────────────────────────────────
AUDIT_DIR.mkdir(exist_ok=True)

class AuditExporter:
    def __init__(self, audit_dir, export_format="csv"):
        self.audit_dir = Path(audit_dir)
        self.audit_dir.mkdir(exist_ok=True)
        self.export_format = export_format.lower()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit-export")

    def submit(self, audit_rows, active_tickers, run_ts):
        return self.executor.submit(self._export, audit_rows, active_tickers, run_ts)

    def _export(self, audit_rows, active_tickers, run_ts):
        audit_df = pd.DataFrame(audit_rows)
        base_name = f"breadth_audit_{run_ts}"
        if self.export_format == "json":
            audit_path = self.audit_dir / f"{base_name}.json"
            audit_df.to_json(audit_path, orient="records", indent=2, force_ascii=False)
        else:
            audit_path = self.audit_dir / f"{base_name}.csv"
            audit_df.to_csv(audit_path, index=False, encoding="utf-8-sig")

        universe_path = self.audit_dir / "active_universe.txt"
        with open(universe_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(active_tickers)))
            handle.write("\n" if active_tickers else "")

        log(f"Audit export complete: {audit_path.name} | active_universe.txt")
        return audit_path

    def shutdown(self):
        self.executor.shutdown(wait=True)

# ─── STEP 3: Calculate breadth ───────────────────────────────────────────────
def calculate_breadth(price_data, sessions_show=50):
    # Build wide close-price DataFrame: rows=dates, cols=tickers
    frames = []
    for ticker, df in price_data.items():
        tmp = df.set_index("time")["close"].rename(ticker)
        tmp = tmp[~tmp.index.duplicated(keep="last")]
        frames.append(tmp)

    if not frames:
        return pd.DataFrame(columns=[ma_key(period) for period in MA_PERIODS])

    prices = pd.concat(frames, axis=1).sort_index()
    prices.index = pd.to_datetime(prices.index)

    # Forward-fill single-day gaps (VN market sometimes has data gaps)
    prices = prices.ffill(limit=2)

    result_rows = []
    for p in MA_PERIODS:
        sma = prices.rolling(window=p, min_periods=p).mean()
        above = (prices > sma)
        n_above = above.sum(axis=1)
        n_total = sma.notna().sum(axis=1)   # only count tickers with enough history
        pct = (n_above / n_total.replace(0, np.nan) * 100).round(2)
        result_rows.append(pct.rename(ma_key(p)))

    breadth = pd.concat(result_rows, axis=1).dropna(how="all")
    breadth = breadth.tail(sessions_show)
    return breadth

# ─── STEP 4: Weekly analysis text ────────────────────────────────────────────
def generate_analysis(breadth, price_data):
    last = breadth.iloc[-1]
    prev = breadth.iloc[-2] if len(breadth) >= 2 else last
    week_ago = breadth.iloc[-6] if len(breadth) >= 6 else breadth.iloc[0]
    last_date = breadth.index[-1].strftime("%d/%m/%Y")

    def signal(v):
        if v is None or pd.isna(v): return "N/A", "gray"
        if v < 20:  return "Quá bán nặng", "#E53935"
        if v < 35:  return "Yếu", "#FF7043"
        if v < 50:  return "Trung tính thấp", "#FFA726"
        if v < 65:  return "Trung tính cao", "#66BB6A"
        if v < 80:  return "Mạnh", "#43A047"
        return "Quá mua", "#1E88E5"

    rows = []
    for p in MA_PERIODS:
        k = ma_key(p)
        v = last.get(k)
        pv = prev.get(k)
        wv = week_ago.get(k)
        sig, col = signal(v)
        delta_day  = round(v - pv, 2) if (v is not None and pv is not None and not pd.isna(v) and not pd.isna(pv)) else 0
        delta_week = round(v - wv, 2) if (v is not None and wv is not None and not pd.isna(v) and not pd.isna(wv)) else 0
        arrow_d = "▲" if delta_day > 0 else ("▼" if delta_day < 0 else "─")
        arrow_w = "▲" if delta_week > 0 else ("▼" if delta_week < 0 else "─")
        rows.append({
            "period": f"SMA-{p}", "key": k, "value": round(v, 2) if v and not pd.isna(v) else "N/A",
            "signal": sig, "color": col,
            "delta_day": f"{arrow_d} {abs(delta_day):.2f}%",
            "delta_week": f"{arrow_w} {abs(delta_week):.2f}%",
        })

    # Composite market score (weighted average)
    weights = {3: 0.05, 5: 0.10, 10: 0.15, 20: 0.20, 50: 0.25, 200: 0.25}
    total_w = 0
    score = 0
    for p in MA_PERIODS:
        k = ma_key(p)
        v = last.get(k)
        if v is not None and not pd.isna(v):
            score += v * weights[p]
            total_w += weights[p]
    composite = round(score / total_w, 1) if total_w > 0 else 0

    # Trend direction (5-session slope of mbz50)
    if len(breadth) >= 6:
        mbz50_recent = breadth[ma_key(50)].dropna().tail(6)
        slope50 = (mbz50_recent.iloc[-1] - mbz50_recent.iloc[0]) / max(len(mbz50_recent) - 1, 1)
    else:
        slope50 = 0

    # mbz03 momentum (fast indicator)
    mbz03_now = last.get(ma_key(3), 50)
    mbz50_now = last.get(ma_key(50), 0)
    mbz200_now = last.get(ma_key(200), 0)

    # Overall verdict
    if composite >= 60:
        verdict = "🟢 TÍCH CỰC — Thị trường rộng, đa số CP trên MA. Có thể tiếp tục nắm giữ/mua vào."
        verdict_color = "#43A047"
    elif composite >= 40:
        verdict = "🟡 TRUNG TÍNH — Thị trường phân hóa. Chọn lọc cổ phiếu mạnh, hạn chế mua đuổi."
        verdict_color = "#FFA726"
    elif composite >= 20:
        verdict = "🟠 THẬN TRỌNG — Phần lớn CP dưới MA. Theo dõi tín hiệu phục hồi trước khi vào hàng."
        verdict_color = "#FF7043"
    else:
        verdict = "🔴 TIÊU CỰC — Thị trường rất yếu. Ưu tiên phòng thủ, chờ mbz50 > 20% để xác nhận đáy."
        verdict_color = "#E53935"

    # Next-week outlook based on mbz03 momentum vs mbz50 trend
    if mbz03_now > 50 and slope50 > 0:
        next_week = "Tuần tới: Đà ngắn hạn đang phục hồi và mbz50 bắt đầu tăng → khả năng tiếp diễn tích cực."
    elif mbz03_now > 50 and slope50 <= 0:
        next_week = "Tuần tới: Ngắn hạn hồi phục nhưng xu hướng trung hạn (SMA-50) chưa xác nhận → cẩn thận bẫy hồi."
    elif mbz03_now <= 50 and slope50 > 0:
        next_week = "Tuần tới: Ngắn hạn chậm lại nhưng mbz50 đang cải thiện → theo dõi sát, có thể tích lũy từng bước."
    else:
        next_week = "Tuần tới: Cả ngắn hạn và trung hạn đều yếu → giữ tỷ trọng thấp, chờ xác nhận đảo chiều."

    return {
        "rows": rows,
        "composite": composite,
        "verdict": verdict,
        "verdict_color": verdict_color,
        "next_week": next_week,
        "last_date": last_date,
        "n_tickers": len(price_data),
    }

# ─── STEP 5: Build HTML ───────────────────────────────────────────────────────
def build_html(
    breadth,
    analysis,
    tickers,
    provider_label,
    vnindex_df=None,
    rs_payload=None,
    drift_payload=None,
    price_data=None,
    us_vix_df=None,
    us_nasdaq_df=None,
):
    dates = [d.strftime("%d-%m-%Y") for d in breadth.index]

    traces = []
    for p in MA_PERIODS:
        k = ma_key(p)
        vals = breadth[k].tolist() if k in breadth.columns else []
        vals_clean = [round(v, 2) if not pd.isna(v) else None for v in vals]
        line_style = {"color": MA_COLORS[p], "width": 4 if p == 50 else 2}
        if p in (3, 5):
            line_style["dash"] = "dot"
        traces.append({
            "x": dates, "y": vals_clean,
            "name": k, "mode": "lines+markers",
            "line": line_style,
            "marker": {"size": 4, "color": MA_COLORS[p]},
            "connectgaps": False,
        })

    chart_data = json.dumps(traces)

    # VNIndex chart data
    vni_chart_data = "null"
    vni_vol_data = "null"
    if vnindex_df is not None and len(vnindex_df) > 0:
        # Align to same date range as breadth
        vni = vnindex_df.copy()
        vni["time"] = pd.to_datetime(vni["time"])
        vni = vni[vni["time"] >= breadth.index[0]].tail(SESSIONS_SHOW)
        vni_dates = [d.strftime("%d-%m-%Y") for d in vni["time"]]
        candle = {
            "type": "candlestick",
            "x": vni_dates,
            "open":  vni["open"].round(2).tolist(),
            "high":  vni["high"].round(2).tolist(),
            "low":   vni["low"].round(2).tolist(),
            "close": vni["close"].round(2).tolist(),
            "name": "VNINDEX",
            "increasing": {"line": {"color": "#43A047"}, "fillcolor": "#43A047"},
            "decreasing": {"line": {"color": "#E53935"}, "fillcolor": "#E53935"},
        }
        colors = ["#43A047" if c >= o else "#E53935"
                  for c, o in zip(vni["close"], vni["open"])]
        vol_bar = {
            "type": "bar",
            "x": vni_dates,
            "y": (vni["volume"] / 1e6).round(1).tolist(),
            "name": "Volume (triệu)",
            "marker": {"color": colors, "opacity": 0.6},
            "xaxis": "x",
            "yaxis": "y2",
        }
        vni_chart_data = json.dumps([candle])
        vni_vol_data   = json.dumps([vol_bar])

    # CBOE VIX volatility index chart data (100 sessions)
    vix_chart_data = "null"
    vix_vol_data = "null"
    if us_vix_df is not None and not us_vix_df.empty:
        vix = us_vix_df.copy()
        vix["time"] = pd.to_datetime(vix["time"])
        vix = vix.tail(US_INDEX_SESSIONS)
        if not vix.empty:
            vix_dates = [d.strftime("%d-%m-%Y") for d in vix["time"]]
            vix_candle = {
                "type": "candlestick",
                "x": vix_dates,
                "open": vix["open"].round(2).tolist(),
                "high": vix["high"].round(2).tolist(),
                "low": vix["low"].round(2).tolist(),
                "close": vix["close"].round(2).tolist(),
                "name": "CBOE VIX",
                "increasing": {"line": {"color": "#43A047"}, "fillcolor": "#43A047"},
                "decreasing": {"line": {"color": "#E53935"}, "fillcolor": "#E53935"},
            }
            vix_colors = [
                "#43A047" if c >= o else "#E53935"
                for c, o in zip(vix["close"], vix["open"])
            ]
            vix_bar = {
                "type": "bar",
                "x": vix_dates,
                "y": vix["volume"].fillna(0).round(0).tolist(),
                "name": "Volume",
                "marker": {"color": vix_colors, "opacity": 0.6},
                "xaxis": "x",
                "yaxis": "y2",
            }
            vix_chart_data = json.dumps([vix_candle])
            vix_vol_data = json.dumps([vix_bar])

    # Nasdaq Composite (^IXIC) chart data (100 sessions)
    ndx_chart_data = "null"
    ndx_vol_data = "null"
    if us_nasdaq_df is not None and not us_nasdaq_df.empty:
        ndx = us_nasdaq_df.copy()
        ndx["time"] = pd.to_datetime(ndx["time"])
        ndx = ndx.tail(US_INDEX_SESSIONS)
        if not ndx.empty:
            ndx_dates = [d.strftime("%d-%m-%Y") for d in ndx["time"]]
            ndx_candle = {
                "type": "candlestick",
                "x": ndx_dates,
                "open": ndx["open"].round(2).tolist(),
                "high": ndx["high"].round(2).tolist(),
                "low": ndx["low"].round(2).tolist(),
                "close": ndx["close"].round(2).tolist(),
                "name": "Nasdaq Composite",
                "increasing": {"line": {"color": "#43A047"}, "fillcolor": "#43A047"},
                "decreasing": {"line": {"color": "#E53935"}, "fillcolor": "#E53935"},
            }
            ndx_colors = [
                "#43A047" if c >= o else "#E53935"
                for c, o in zip(ndx["close"], ndx["open"])
            ]
            ndx_bar = {
                "type": "bar",
                "x": ndx_dates,
                "y": (ndx["volume"].fillna(0) / 1e9).round(2).tolist(),
                "name": "Volume (tỷ)",
                "marker": {"color": ndx_colors, "opacity": 0.6},
                "xaxis": "x",
                "yaxis": "y2",
            }
            ndx_chart_data = json.dumps([ndx_candle])
            ndx_vol_data = json.dumps([ndx_bar])

    # Build analysis table rows HTML
    table_rows = ""
    for r in analysis["rows"]:
        table_rows += f"""
        <tr>
          <td style="font-weight:600;color:{MA_COLORS[int(r['key'].replace('mbz','') or 3)]}">{r['period']}</td>
          <td style="text-align:center;font-weight:700">{r['value']}%</td>
          <td style="text-align:center"><span style="color:{r['color']};font-weight:600">{r['signal']}</span></td>
          <td style="text-align:center">{r['delta_day']}</td>
          <td style="text-align:center">{r['delta_week']}</td>
        </tr>"""

    # Period color items for formula section
    formula_items = ""
    for p in MA_PERIODS:
        formula_items += f'<li><b style="color:{MA_COLORS[p]}">{ma_key(p)}</b> = (Số CP có Giá đóng cửa &gt; SMA-{p}) ÷ Tổng CP có đủ dữ liệu × 100</li>'

    rs_dates = rs_payload["dates"] if rs_payload else []
    rs_date_headers = "".join(f"<th>{date_label}</th>" for date_label in rs_dates)
    rs_rows_html = ""
    if rs_payload:
        for row in rs_payload["rows"]:
            cells_html = ""
            for cell in row["cells"]:
                rs_rating = cell["rs_rating"]
                momentum_rating = cell["weighted_momentum_rating"]
                momentum_score = cell["weighted_momentum_score"]
                daily_change = cell["daily_change_pct"]
                if rs_rating is None:
                    tone_class = "rs-cell rs-empty"
                    rs_text = "–"
                    change_text = ""
                    change_class = "rs-change"
                else:
                    if rs_rating >= 90:
                        tone_class = "rs-cell rs-leader"
                    elif rs_rating >= 70:
                        tone_class = "rs-cell rs-strong"
                    elif rs_rating >= 50:
                        tone_class = "rs-cell rs-neutral"
                    else:
                        tone_class = "rs-cell rs-laggard"
                    rs_text = str(rs_rating)
                    if daily_change is not None and not pd.isna(daily_change):
                        change_text = f"{daily_change:+.2f}%"
                        if daily_change > 0:
                            change_class = "rs-change rs-change-up"
                        elif daily_change < 0:
                            change_class = "rs-change rs-change-down"
                        else:
                            change_class = "rs-change rs-change-flat"
                    else:
                        change_text = ""
                        change_class = "rs-change"
                cells_html += (
                    f'<td class="{tone_class}" data-rs="{rs_text}">'
                    f'<div class="rs-score">{rs_text}</div>'
                    f'<div class="{change_class}">{change_text}</div>'
                    f"</td>"
                )

            rs_rows_html += (
                f'<tr data-ticker="{row["ticker"]}">'
                f'<td class="rs-ticker">{row["ticker"]}</td>'
                f"{cells_html}</tr>"
            )

    rs_section_html = ""
    if rs_payload:
        source_label_class = "rs-source-label rs-source-ok" if rs_payload["source_label_is_ok"] else "rs-source-label rs-source-bad"
        source_label_title = rs_payload["source_label_tooltip"] if rs_payload["source_label_tooltip"] else rs_payload["source_label"]
        rs_section_html = f"""
  <div class="panel">
    <h2>Relative Strength Heatmap <span class="tag">Institutional 3T</span></h2>
    <div class="rs-toolbar">
      <input id="rs-search" class="rs-search" type="text" placeholder="Tìm mã cổ phiếu...">
      <div class="rs-toolbar-note">RS 90 ngày so với VNINDEX | mới nhất trước | dòng dưới = % giá thay đổi so với phiên trước</div>
    </div>
    <div class="{source_label_class}" title="{source_label_title}">{rs_payload['source_label']}</div>
    <div class="rs-table-wrap">
      <table class="rs-table" id="rs-table">
        <thead>
          <tr>
            <th class="rs-sticky-col">Ticker</th>
            {rs_date_headers}
          </tr>
        </thead>
        <tbody>
          {rs_rows_html}
        </tbody>
      </table>
    </div>
    <div class="rs-footer">{rs_payload['footer_text']}</div>
  </div>
"""
    rs_footer_warning = ""
    if rs_payload and not rs_payload["is_complete"]:
        rs_footer_warning = (
            f"Warning: RS Matrix incomplete ({rs_payload['audit_confirmed_count']}/{rs_payload['expected_ticker_count']} tickers confirmed)"
        )

    # Universe Drift Alert banner is suppressed because the unified locked
    # universe (rs_fixed_tickers.csv = 230 tickers) intentionally exceeds the
    # institutional 3T scan (172 tickers) by 58 manual pre-breakout/breadth
    # additions, which the drift detector reports as false-positive removals.
    # The drift script still runs and writes logs/universe_drift_*.txt for audit.
    drift_notification_html = ""

    now_str = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")
    session_label = datetime.now(ICT).strftime("%d/%m/%Y")

    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Cơ hội - Độ rộng thị trường Việt Nam</title>
  <script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f5f0e8;
      color: #222;
      min-height: 100vh;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
    h1 {{
      text-align: center;
      color: #c0392b;
      font-size: 1.6rem;
      margin-bottom: 4px;
    }}
    .subtitle {{
      text-align: center;
      color: #777;
      font-size: 0.85rem;
      margin-bottom: 16px;
    }}
    .session-banner {{
      display: flex;
      justify-content: flex-end;
      margin-bottom: 8px;
    }}
    .session-pill {{
      background: #fff3cd;
      border: 1px solid #f0c36d;
      border-radius: 999px;
      color: #8a5a00;
      font-size: 0.8rem;
      font-weight: 700;
      padding: 6px 12px;
    }}
    #chart {{
      background: #fff8f0;
      border: 1px solid #e0d8cc;
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 24px;
    }}
    .panel {{
      background: #fff;
      border: 1px solid #e0d0c0;
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 20px;
    }}
    .panel h2 {{
      font-size: 1.1rem;
      margin-bottom: 14px;
      padding-bottom: 8px;
      border-bottom: 2px solid #f0e8dc;
      color: #333;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th {{
      background: #f0e8dc;
      padding: 8px 12px;
      text-align: left;
      font-weight: 600;
      color: #444;
    }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #f5f0ea; }}
    tr:last-child td {{ border-bottom: none; }}
    table:not(.rs-table) tr:hover td {{ background: #fdf8f4; }}
    .verdict-box {{
      padding: 14px 18px;
      border-radius: 6px;
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 12px;
      border-left: 5px solid {analysis['verdict_color']};
      background: #fafafa;
      color: {analysis['verdict_color']};
    }}
    .next-week-box {{
      padding: 12px 16px;
      background: #f0f8ff;
      border-left: 4px solid #1E88E5;
      border-radius: 4px;
      color: #1565C0;
      font-size: 0.95rem;
    }}
    .composite {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .gauge {{
      width: 80px; height: 80px;
      border-radius: 50%;
      background: conic-gradient(
        {analysis['verdict_color']} {analysis['composite'] * 3.6}deg,
        #e0e0e0 {analysis['composite'] * 3.6}deg
      );
      display: flex; align-items: center; justify-content: center;
      font-size: 1.3rem;
      font-weight: 700;
      color: #222;
      position: relative;
    }}
    .gauge::before {{
      content: '';
      position: absolute;
      width: 58px; height: 58px;
      background: #fff;
      border-radius: 50%;
    }}
    .gauge span {{ position: relative; z-index: 1; font-size: 1rem; }}
    .formula-list {{ font-size: 0.88rem; line-height: 2; }}
    .formula-list li {{ list-style: none; padding: 2px 0; }}
    .note {{ font-size: 0.8rem; color: #888; margin-top: 10px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    @media (max-width: 700px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
    .tag {{
      display: inline-block;
      background: #f0e8dc;
      border-radius: 4px;
      padding: 2px 8px;
      font-size: 0.8rem;
      color: #666;
      margin-left: 6px;
    }}
    .rs-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .rs-search {{
      min-width: 220px;
      border: 1px solid #d7cab8;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 0.9rem;
      background: #fffdf9;
    }}
    .rs-toolbar-note {{
      color: #666;
      font-size: 0.85rem;
      font-weight: 600;
    }}
    .rs-footer {{
      margin-top: 12px;
      color: #5f6b7a;
      font-size: 0.85rem;
      font-weight: 600;
    }}
    .rs-source-label {{
      display: inline-block;
      margin-bottom: 12px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 700;
      border: 1px solid transparent;
    }}
    .rs-source-ok {{
      color: #2E7D32;
      background: #E8F5E9;
      border-color: #A5D6A7;
    }}
    .rs-source-bad {{
      color: #C62828;
      background: #FFEBEE;
      border-color: #EF9A9A;
      cursor: help;
    }}
    .rs-table-wrap {{
      max-height: 800px;
      overflow: auto;
      border: 1px solid #ead8c0;
      border-radius: 8px;
      background: #fffdf9;
    }}
    .rs-table {{
      width: max-content;
      min-width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 0.82rem;
    }}
    .rs-table thead th {{
      position: sticky;
      top: 0;
      z-index: 3;
      background: #f5e6cf;
      text-align: center;
      white-space: nowrap;
    }}
    .rs-table .rs-sticky-col {{
      position: sticky;
      left: 0;
      z-index: 4;
      background: #f8efe2;
      min-width: 84px;
      text-align: left;
    }}
    .rs-table td.rs-ticker {{
      position: sticky;
      left: 0;
      z-index: 2;
      background: #fff8f0;
      font-weight: 700;
      min-width: 84px;
    }}
    .rs-table td, .rs-table th {{
      border-bottom: 1px solid #f0e6da;
      border-right: 1px solid #f0e6da;
      padding: 8px 10px;
    }}
    .rs-cell {{
      min-width: 76px;
      text-align: center;
      font-weight: 700;
    }}
    .rs-score {{
      font-size: 1rem;
      line-height: 1.1;
      color: #fff;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.55);
    }}
    .rs-change {{
      font-size: 0.72rem;
      line-height: 1.1;
      margin-top: 4px;
      display: inline-block;
      min-width: 54px;
      padding: 2px 5px;
      border-radius: 4px;
      opacity: 1;
    }}
    .rs-change-up {{
      color: #052E16;
      background: #86EFAC;
    }}
    .rs-change-down {{
      color: #7F1D1D;
      background: #FECACA;
    }}
    .rs-change-flat {{
      color: #111827;
      background: #E5E7EB;
    }}
    .rs-leader {{
      background: #6D28D9;
      color: #fff;
    }}
    .rs-strong {{
      background: #047857;
      color: #fff;
    }}
    .rs-neutral {{
      background: #FBC02D;
      color: #4E3D00;
    }}
    .rs-laggard {{
      background: #DC2626;
      color: #fff;
    }}
    .rs-table tbody tr:hover td.rs-ticker {{
      background: #fff8f0;
    }}
    .rs-table tbody tr:hover td.rs-leader {{
      background: #6D28D9;
    }}
    .rs-table tbody tr:hover td.rs-strong {{
      background: #047857;
    }}
    .rs-table tbody tr:hover td.rs-neutral {{
      background: #FBC02D;
    }}
    .rs-table tbody tr:hover td.rs-laggard {{
      background: #DC2626;
    }}
    .rs-table tbody tr:hover td.rs-empty {{
      background: #f6f1eb;
    }}
    .rs-empty {{
      background: #f6f1eb;
      color: #b2a79a;
    }}
    .drift-alert {{
      margin-bottom: 20px;
      padding: 14px 16px;
      border-radius: 8px;
      border: 1px solid #f0c36d;
      background: #fff7df;
      color: #8a5a00;
      font-size: 0.9rem;
      line-height: 1.6;
    }}
    .drift-alert-detail {{
      margin-top: 8px;
      color: #6d4f00;
      font-size: 0.85rem;
    }}
  </style>
</head>
<body>
<div class="container">
  <div class="session-banner">
    <div class="session-pill">Market Session: {session_label} | Final EOD Settlement</div>
  </div>
  <h1>📊 Cơ hội - Độ rộng thị trường (50 phiên)</h1>
  <div class="subtitle">Top 100 cổ phiếu vốn hóa lớn HOSE + HNX &nbsp;|&nbsp; Cập nhật: {now_str} &nbsp;|&nbsp; {analysis['n_tickers']} CP có dữ liệu</div>

  {drift_notification_html}

  <div id="chart"></div>

  <div id="vnindex-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>

  <div id="vix-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>

  <div id="nasdaq-chart" style="background:#fff8f0;border:1px solid #e0d8cc;border-radius:8px;padding:10px;margin-bottom:24px"></div>

  <div class="panel">
    <h2>🧮 Nhận định tuần tới <span class="tag">{analysis['last_date']}</span></h2>
    <div class="composite">
      <div class="gauge"><span>{analysis['composite']}</span></div>
      <div>
        <div style="font-size:0.8rem;color:#888;margin-bottom:4px">Điểm tổng hợp (0–100)</div>
        <div class="verdict-box">{analysis['verdict']}</div>
      </div>
    </div>
    <div class="next-week-box">{analysis['next_week']}</div>
  </div>

  <div class="grid-2">
    <div class="panel">
      <h2>📋 Chi tiết chỉ số</h2>
      <table>
        <thead>
          <tr>
            <th>Chỉ số</th>
            <th style="text-align:center">Hiện tại</th>
            <th style="text-align:center">Trạng thái</th>
            <th style="text-align:center">Δ ngày</th>
            <th style="text-align:center">Δ tuần</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      <p class="note">▲/▼ = thay đổi so với phiên trước / 5 phiên trước</p>
    </div>

    <div class="panel">
      <h2>📐 Công thức tính</h2>
      <p style="font-size:0.88rem;margin-bottom:10px;color:#555">
        <b>mbzN</b> = (Số cổ phiếu trong top-100 có <em>Giá đóng cửa &gt; SMA(N)</em>) ÷ Tổng cổ phiếu có đủ dữ liệu × 100
      </p>
      <ul class="formula-list">
        {formula_items}
      </ul>
      <p class="note" style="margin-top:14px">
        <b>Cách đọc:</b><br>
        &gt; 80%: Quá mua 🔵 &nbsp;|&nbsp; 60–80%: Mạnh 🟢 &nbsp;|&nbsp; 40–60%: Trung tính 🟡<br>
        20–40%: Yếu 🟠 &nbsp;|&nbsp; &lt; 20%: Quá bán 🔴 (cơ hội mua)
      </p>
    </div>
  </div>


  <div class="panel">
    <h2>📚 Giải thích mbz200 &gt; mbz50 hiện tại</h2>
    <p style="font-size:0.88rem;color:#555;line-height:1.7">
      Khi <b>mbz50 &lt; mbz200</b>, thị trường đang trong giai đoạn <em>điều chỉnh trung hạn nhưng xu hướng dài hạn còn tích cực</em>.
      Nghĩa là nhiều CP đã phá vỡ SMA-50 (bán tháo gần đây) nhưng vẫn duy trì được trên SMA-200 (nền tảng dài hạn).
      Đây thường là <b>vùng tích lũy tốt</b> cho nhà đầu tư dài hạn, nhưng cần chờ mbz50 hồi phục &gt;20% để xác nhận đà phục hồi bền vững.
    </p>
  </div>

  {rs_section_html}

  <p class="note" style="text-align:center;padding-bottom:20px">
    Dữ liệu: vnstock EOD ({provider_label}) &nbsp;|&nbsp; Vũ trụ: {analysis['n_tickers']} CP top-100 HOSE+HNX &nbsp;|&nbsp;
    {rs_payload['footer_text'] if rs_payload else 'Vũ trụ RS chưa sẵn sàng'} &nbsp;|&nbsp;
    Chạy lại script để cập nhật &nbsp;|&nbsp; Cache: {CACHE_HOURS}h
  </p>
  <p class="note" style="text-align:center;padding-bottom:20px;color:#c0392b;font-weight:700">{rs_footer_warning}</p>
</div>

<script>
const traces = {chart_data};
const layout = {{
  title: {{ text: 'Cơ hội - 50 phiên', font: {{ color: '#c0392b', size: 18 }} }},
  paper_bgcolor: '#fff8f0',
  plot_bgcolor: '#fff8f0',
  xaxis: {{
    type: 'category',
    tickangle: -45,
    tickfont: {{ size: 10 }},
    gridcolor: '#ead8c0',
    showgrid: true,
  }},
  yaxis: {{
    range: [0, 105],
    ticksuffix: '%',
    gridcolor: '#ead8c0',
    showgrid: true,
  }},
  legend: {{ orientation: 'h', y: -0.25, x: 0.5, xanchor: 'center' }},
  hovermode: 'x unified',
  margin: {{ l: 50, r: 80, t: 60, b: 120 }},
  height: 624,
}};
const config = {{ responsive: true, displayModeBar: true }};
Plotly.newPlot('chart', traces, layout, config);

// VNIndex chart
const vniData = {vni_chart_data};
const vniVol  = {vni_vol_data};
if (vniData && vniVol) {{
  const vniLayout = {{
    title: {{ text: 'VN-Index - 50 phiên', font: {{ color: '#c0392b', size: 18 }} }},
    paper_bgcolor: '#fff8f0',
    plot_bgcolor: '#fff8f0',
    xaxis: {{
      type: 'category',
      tickangle: -45,
      tickfont: {{ size: 10 }},
      gridcolor: '#ead8c0',
      rangeslider: {{ visible: false }},
      anchor: 'y2',
      domain: [0, 1],
    }},
    yaxis: {{
      title: 'Điểm',
      gridcolor: '#ead8c0',
      domain: [0.30, 1],
    }},
    yaxis2: {{
      title: 'KL (triệu)',
      showgrid: false,
      domain: [0, 0.25],
    }},
    legend: {{ orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center' }},
    hovermode: 'x unified',
    margin: {{ l: 60, r: 60, t: 60, b: 100 }},
    height: 624,
  }};
  Plotly.newPlot('vnindex-chart', [...vniData, ...vniVol], vniLayout, config);
}}

// VIX chart
const vixData = {vix_chart_data};
const vixVol  = {vix_vol_data};
if (vixData && vixVol) {{
  const vixLayout = {{
    title: {{ text: 'CBOE VIX - 100 phiên', font: {{ color: '#c0392b', size: 18 }} }},
    paper_bgcolor: '#fff8f0',
    plot_bgcolor: '#fff8f0',
    xaxis: {{
      type: 'category',
      tickangle: -45,
      tickfont: {{ size: 10 }},
      gridcolor: '#ead8c0',
      rangeslider: {{ visible: false }},
      anchor: 'y2',
      domain: [0, 1],
    }},
    yaxis: {{
      title: 'VIX',
      gridcolor: '#ead8c0',
      domain: [0.30, 1],
    }},
    yaxis2: {{
      title: 'Volume',
      showgrid: false,
      domain: [0, 0.25],
    }},
    legend: {{ orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center' }},
    hovermode: 'x unified',
    margin: {{ l: 60, r: 60, t: 60, b: 100 }},
    height: 624,
  }};
  Plotly.newPlot('vix-chart', [...vixData, ...vixVol], vixLayout, config);
}} else {{
  const vixChart = document.getElementById('vix-chart');
  if (vixChart) {{
    vixChart.style.display = 'none';
  }}
}}

// Nasdaq Composite chart (100 sessions)
const ndxData = {ndx_chart_data};
const ndxVol  = {ndx_vol_data};
if (ndxData && ndxVol) {{
  const ndxLayout = {{
    title: {{ text: 'Nasdaq Composite (^IXIC) - 100 phiên', font: {{ color: '#c0392b', size: 18 }} }},
    paper_bgcolor: '#fff8f0',
    plot_bgcolor: '#fff8f0',
    xaxis: {{
      type: 'category',
      tickangle: -45,
      tickfont: {{ size: 10 }},
      gridcolor: '#ead8c0',
      rangeslider: {{ visible: false }},
      anchor: 'y2',
      domain: [0, 1],
    }},
    yaxis: {{
      title: 'Điểm',
      gridcolor: '#ead8c0',
      domain: [0.30, 1],
    }},
    yaxis2: {{
      title: 'KL (tỷ)',
      showgrid: false,
      domain: [0, 0.25],
    }},
    legend: {{ orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center' }},
    hovermode: 'x unified',
    margin: {{ l: 60, r: 60, t: 60, b: 100 }},
    height: 624,
  }};
  Plotly.newPlot('nasdaq-chart', [...ndxData, ...ndxVol], ndxLayout, config);
}} else {{
  const ndxChart = document.getElementById('nasdaq-chart');
  if (ndxChart) {{
    ndxChart.style.display = 'none';
  }}
}}

const rsSearch = document.getElementById('rs-search');
if (rsSearch) {{
  rsSearch.addEventListener('input', (event) => {{
    const query = event.target.value.trim().toUpperCase();
    document.querySelectorAll('#rs-table tbody tr').forEach((row) => {{
      const ticker = row.getAttribute('data-ticker') || '';
      row.style.display = !query || ticker.includes(query) ? '' : 'none';
    }});
  }});
}}
</script>
</body>
</html>
"""
    return html

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true", help="Skip opening browser (for CI)")
    args = parser.parse_args()

    audit_exporter = AuditExporter(AUDIT_DIR, export_format=AUDIT_EXPORT_FORMAT)
    log("=== Vietnam Market Breadth Generator ===")

    try:
        run_started_at = time.perf_counter()
        combined_path, combined_modified_at = verify_fresh_eod_dataset()
        log(
            "Verified fresh EOD dataset: "
            f"{combined_path} | modified {combined_modified_at.strftime('%d/%m/%Y %H:%M:%S %Z')}"
        )
        tickers = read_tickers()
        combined_df, price_data, provider_label, vnindex_df = load_price_data_from_combined_dataset(combined_path)
        active_tickers = sorted(price_data.keys())
        failed_tickers = [ticker for ticker in tickers if ticker not in price_data]
        log("Zero-refetch policy active: loading breadth inputs from combined_dataset.csv")
        log(f"Providers used for this breadth run: {provider_label}")
        log(f"Successful tickers from dataset: {len(price_data)} | Missing tickers: {len(failed_tickers)}")
        if failed_tickers:
            log(f"Tickers missing from dataset: {', '.join(failed_tickers)}")
        if vnindex_df is None or vnindex_df.empty:
            log("WARNING: VN-Index data missing from combined_dataset.csv. VN-Index chart will be empty.")
        else:
            required_vni_columns = {"time", "open", "high", "low", "close", "volume"}
            missing_vni_columns = required_vni_columns - set(vnindex_df.columns)
            if missing_vni_columns:
                log(
                    "WARNING: VN-Index data is incomplete in combined_dataset.csv. "
                    f"Missing columns: {', '.join(sorted(missing_vni_columns))}. "
                    "VN-Index chart may be empty."
                )
            else:
                log(f"VN-Index rows loaded from dataset: {len(vnindex_df)}")

        if len(price_data) < MIN_SUCCESSFUL_TICKERS:
            raise RuntimeError(
                f"Critical error: only {len(price_data)} tickers fetched successfully. "
                f"Need at least {MIN_SUCCESSFUL_TICKERS} to draw the breadth chart."
            )

        run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log("Queueing audit export in background ...")
        audit_rows = []
        latest_rows = combined_df.sort_values("time").groupby("ticker", sort=True).tail(1)
        for _, row in latest_rows.sort_values("ticker").iterrows():
            audit_rows.append({
                "Ticker Symbol": row["ticker"],
                "Price": round(float(row["close"]), 4) if pd.notna(row["close"]) else None,
                "Change %": round(float(row["change_pct"]), 4) if "change_pct" in row and pd.notna(row["change_pct"]) else None,
                "Volume": int(row["volume"]) if pd.notna(row["volume"]) else None,
                "Status": "Success",
                "Data Source": row["source"],
            })
        for ticker in failed_tickers:
            audit_rows.append({
                "Ticker Symbol": ticker,
                "Price": None,
                "Change %": None,
                "Volume": None,
                "Status": "Missing From Combined Dataset",
                "Data Source": "n/a",
            })
        audit_future = audit_exporter.submit(audit_rows, active_tickers, run_ts)

        log("Calculating breadth indicators ...")
        breadth = calculate_breadth(price_data, SESSIONS_SHOW)
        log(f"Breadth matrix: {breadth.shape[0]} sessions x {breadth.shape[1]} indicators")

        log("Generating analysis ...")
        analysis = generate_analysis(breadth, price_data)
        log(f"Composite score: {analysis['composite']} | {analysis['verdict']}")
        log("Zero-refetch policy active: using VN-Index data from combined_dataset.csv")
        if vnindex_df is not None and not vnindex_df.empty:
            aligned_vnindex = vnindex_df.copy()
            aligned_vnindex["time"] = pd.to_datetime(aligned_vnindex["time"])
            aligned_vnindex = aligned_vnindex[aligned_vnindex["time"] >= breadth.index[0]]
            if aligned_vnindex.empty:
                log("WARNING: VN-Index data exists in combined_dataset.csv but does not overlap the breadth window.")

        rs_payload = load_rs_matrix_payload()
        if rs_payload:
            log(
                f"RS heatmap loaded: {rs_payload['row_count']} tickers x "
                f"{len(rs_payload['dates'])} sessions"
            )
            if not rs_payload["is_complete"]:
                log(
                    "WARNING: RS Matrix incomplete "
                    f"({rs_payload['audit_confirmed_count']}/{rs_payload['expected_ticker_count']} tickers confirmed)"
                )

        drift_payload = load_universe_drift_payload()
        if drift_payload:
            log(
                f"Universe drift loaded: {drift_payload['total_changes']} changes "
                f"({len(drift_payload['additions'])} additions, {len(drift_payload['removals'])} removals)"
            )
            if drift_payload["is_significant"]:
                log("Universe drift is significant and will be shown on the dashboard.")

        log("Loading CBOE VIX volatility index (100 sessions) ...")
        us_vix_df = load_us_vix_index_data(US_INDEX_SESSIONS)
        if us_vix_df.empty:
            log("WARNING: CBOE VIX chart will be hidden.")
        else:
            log(f"CBOE VIX rows loaded: {len(us_vix_df)}")

        log("Loading Nasdaq Composite (100 sessions) ...")
        us_nasdaq_df = load_us_nasdaq_index_data(US_INDEX_SESSIONS)
        if us_nasdaq_df.empty:
            log("WARNING: Nasdaq chart will be hidden.")
        else:
            log(f"Nasdaq Composite rows loaded: {len(us_nasdaq_df)}")

        log("Building HTML ...")
        html = build_html(
            breadth,
            analysis,
            tickers,
            provider_label,
            vnindex_df,
            rs_payload,
            drift_payload,
            price_data,
            us_vix_df,
            us_nasdaq_df,
        )
        with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"Saved: {OUTPUT_HTML}")

        # Inject pre-breakout panel (Layer A: RS Line divergence; Layer B: RS_Ratio + BB squeeze).
        try:
            import pre_breakout
            from _patch_pre_breakout import build_panel, PANEL_CSS, PANEL_MARKER, PANEL_END
            log("Computing pre-breakout signals ...")
            pb = pre_breakout.compute(combined_path, SCRIPT_DIR / "rs_fixed_tickers.csv")
            log(
                f"Pre-breakout: layer_a={len(pb.layer_a)} watch_a={len(pb.layer_a_watch)} "
                f"layer_b={len(pb.layer_b)} watch_b={len(pb.layer_b_watch)} both={len(pb.both)} "
                f"analyzed={pb.meta['analyzed_count']}/{pb.meta['universe_count']}"
            )
            html_with_panel = OUTPUT_HTML.read_text(encoding="utf-8")
            if "/* Pre-breakout panel */" not in html_with_panel:
                html_with_panel = html_with_panel.replace("</style>", PANEL_CSS + "\n  </style>", 1)
            panel_html = build_panel(pb)
            import re as _re
            if PANEL_MARKER in html_with_panel:
                html_with_panel = _re.sub(
                    _re.escape(PANEL_MARKER) + r".*?" + _re.escape(PANEL_END),
                    panel_html.strip("\n"),
                    html_with_panel, count=1, flags=_re.DOTALL,
                )
            else:
                anchor = '<div class="panel">\n    <h2>Relative Strength Heatmap'
                if anchor in html_with_panel:
                    html_with_panel = html_with_panel.replace(anchor, panel_html + "  " + anchor, 1)
            OUTPUT_HTML.write_text(html_with_panel, encoding="utf-8")
        except Exception as exc:
            log(f"WARNING: Pre-breakout panel injection failed: {exc}")

        html_size = OUTPUT_HTML.stat().st_size
        last_three_tickers = get_last_three_combined_tickers(combined_path)
        log(f"HTML verification | size: {html_size} bytes")
        log(
            "HTML verification | last 3 combined tickers: "
            f"{', '.join(last_three_tickers) if last_three_tickers else 'unavailable'}"
        )
        elapsed_seconds = time.perf_counter() - run_started_at
        log(f"Dataset-driven HTML generation completed in {elapsed_seconds:.2f} seconds")
        if elapsed_seconds > 2.0:
            log("WARNING: HTML generation exceeded 2 seconds.")

        if not args.no_browser and not os.environ.get("GITHUB_ACTIONS"):
            webbrowser.open(OUTPUT_HTML.as_uri())
            log("Done! Browser should open automatically.")
        else:
            log("Done! (browser skipped in CI mode)")

        audit_future.result(timeout=120)
    finally:
        audit_exporter.shutdown()

if __name__ == "__main__":
    main()
