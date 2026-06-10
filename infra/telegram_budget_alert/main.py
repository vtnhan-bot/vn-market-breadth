"""Cloud Function: send Telegram alert when the GCP budget 80% threshold fires.

Triggered by Cloud Billing Budget notifications published to Pub/Sub topic
billing-budget-alerts. Only the 80% threshold fires this notifier; the 100%
threshold is handled separately by billing-killswitch (which unlinks the
project from billing).

Re-link guidance in the alert is parameterized via RELINK_PROJECT /
RELINK_BILLING_ACCOUNT env vars so the same source deploys to any project.

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

ICT = timezone(timedelta(hours=7))


def telegram_alert(event, context):
    pubsub_data = base64.b64decode(event["data"]).decode("utf-8")
    payload = json.loads(pubsub_data)

    threshold = payload.get("alertThresholdExceeded")
    if threshold is None:
        print("No alertThresholdExceeded — periodic spend update; skipping.")
        return

    if abs(float(threshold) - ALERT_THRESHOLD) > 0.001:
        print(f"Threshold {threshold} != target {ALERT_THRESHOLD}; skipping.")
        return

    cost = float(payload.get("costAmount", 0))
    budget = float(payload.get("budgetAmount", 0))
    currency = payload.get("currencyCode", "")
    display_name = payload.get("budgetDisplayName", "(unknown)")
    interval_start = payload.get("costIntervalStart", "")
    pct = (cost / budget * 100.0) if budget else 0.0
    now_ict = datetime.now(ICT).strftime("%H:%M %d/%m/%Y")

    relink = (
        f"`gcloud billing projects link {RELINK_PROJECT} "
        f"--billing-account={RELINK_BILLING_ACCOUNT}`"
        if RELINK_PROJECT and RELINK_BILLING_ACCOUNT
        else "(re-link the project to its billing account to restore service)"
    )

    text = (
        f"\U0001F6A8 *GCP Budget {int(round(float(threshold)*100))}% reached*\n\n"
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
