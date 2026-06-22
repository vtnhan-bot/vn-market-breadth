"""Cloud Function: send Telegram alert on GCP budget threshold notifications.

Triggered by Cloud Billing Budget notifications published to Pub/Sub topic
billing-budget-alerts.

Two firing modes:
  * ALERT_THRESHOLD (default 0.8) — the normal "approaching budget" warning.
  * ALERT_FAILSAFE_MIN (optional) — if set, ALSO fire for ANY threshold >= this
    value. This is the escalation band ABOVE the killswitch point (100%), so the
    user is pinged at every high threshold (e.g. 100/113/133/.../200%) IN CASE
    the killswitch fails to unlink billing. Breadth leaves this unset and stays
    80%-only (per the user's single-threshold preference); bionic sets it to 1.0.

Re-link guidance is parameterized via RELINK_PROJECT / RELINK_BILLING_ACCOUNT so
the same source deploys to any project.

Budget alert payload schema (v1.0):
  {
    "budgetDisplayName": "...",
    "alertThresholdExceeded": 0.8,
    "costAmount": 96000,
    "costIntervalStart": "2026-05-01T00:00:00Z",
    "budgetAmount": 120000,
    "budgetAmountType": "SPECIFIED_AMOUNT",
    "currencyCode": "VND"
  }
Periodic spend updates publish without alertThresholdExceeded — we skip those.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "0.8"))
RELINK_PROJECT = os.environ.get("RELINK_PROJECT", "")
RELINK_BILLING_ACCOUNT = os.environ.get("RELINK_BILLING_ACCOUNT", "")

# Optional escalation band above the killswitch point. Unset on breadth.
_failsafe_raw = os.environ.get("ALERT_FAILSAFE_MIN", "").strip()
FAILSAFE_MIN = float(_failsafe_raw) if _failsafe_raw else None

ICT = timezone(timedelta(hours=7))


def telegram_alert(event, context):
    pubsub_data = base64.b64decode(event["data"]).decode("utf-8")
    payload = json.loads(pubsub_data)

    threshold = payload.get("alertThresholdExceeded")
    if threshold is None:
        print("No alertThresholdExceeded — periodic spend update; skipping.")
        return

    threshold = float(threshold)
    in_target = abs(threshold - ALERT_THRESHOLD) <= 0.001
    in_failsafe = FAILSAFE_MIN is not None and threshold >= FAILSAFE_MIN - 0.001
    if not (in_target or in_failsafe):
        print(
            f"Threshold {threshold} not target {ALERT_THRESHOLD} "
            f"nor failsafe>={FAILSAFE_MIN}; skipping."
        )
        return

    cost = float(payload.get("costAmount", 0))
    budget = float(payload.get("budgetAmount", 0))
    currency = payload.get("currencyCode", "")
    display_name = payload.get("budgetDisplayName", "(unknown)")
    interval_start = payload.get("costIntervalStart", "")
    pct = (cost / budget * 100.0) if budget else 0.0
    now_ict = datetime.now(ICT).strftime("%H:%M %d/%m/%Y")
    pct_label = int(round(threshold * 100))

    # At/above 100% this is the failsafe band: the killswitch SHOULD have
    # unlinked billing at 100%. Tell the user to verify and unlink manually.
    is_failsafe_band = threshold >= 1.0 - 0.001

    if is_failsafe_band:
        console = (
            f"https://console.cloud.google.com/billing/linkedaccount?project={RELINK_PROJECT}"
            if RELINK_PROJECT
            else "the Cloud Console billing page"
        )
        text = (
            f"\U0001F6A8\U0001F6A8 *FAILSAFE: GCP spend at {pct_label}% of budget*\n\n"
            f"*Budget:* {display_name}\n"
            f"*Spent:* {cost:,.0f} {currency} of {budget:,.0f} {currency} ({pct:.1f}%)\n"
            f"*Period start:* {interval_start}\n"
            f"*Notified at:* {now_ict} ICT\n\n"
            f"⚠️ Spend is AT/ABOVE the 100% killswitch point. If billing is "
            f"still ACTIVE, the killswitch did NOT fire — disable billing manually now:\n"
            f"{console}"
        )
    else:
        relink = (
            f"`gcloud billing projects link {RELINK_PROJECT} "
            f"--billing-account={RELINK_BILLING_ACCOUNT}`"
            if RELINK_PROJECT and RELINK_BILLING_ACCOUNT
            else "(re-link the project to its billing account to restore service)"
        )
        text = (
            f"\U0001F6A8 *GCP Budget {pct_label}% reached*\n\n"
            f"*Budget:* {display_name}\n"
            f"*Spent:* {cost:,.0f} {currency} of {budget:,.0f} {currency} ({pct:.1f}%)\n"
            f"*Period start:* {interval_start}\n"
            f"*Notified at:* {now_ict} ICT\n\n"
            f"Killswitch fires at 100% — billing will unlink automatically. "
            f"Re-link via:\n"
            f"{relink}"
        )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Telegram send OK: {resp.read().decode('utf-8')}")
    except Exception as exc:
        print(f"Telegram send FAILED: {exc}")
        raise
