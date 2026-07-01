"""
Cloud Function: disables billing on the project when the budget is exceeded.

Triggered by Cloud Billing Budget notifications published to Pub/Sub.
When the actual cost exceeds the budget (100% threshold or higher), the function
disassociates the project from its billing account, which halts all chargeable
resources. The dashboard goes dark until billing is manually re-linked.

To re-enable after a trigger:
    gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
"""

from __future__ import annotations

import base64
import json
import os

from googleapiclient import discovery

PROJECT_ID = os.environ["GCP_PROJECT_OVERRIDE"]
PROJECT_NAME = f"projects/{PROJECT_ID}"


def stop_billing(event, context):
    pubsub_data = base64.b64decode(event["data"]).decode("utf-8")
    payload = json.loads(pubsub_data)
    cost_amount = payload.get("costAmount", 0)
    budget_amount = payload.get("budgetAmount", 0)

    print(
        f"Budget event: cost={cost_amount} {payload.get('currencyCode', '')} "
        f"budget={budget_amount} threshold={payload.get('alertThresholdExceeded', 'n/a')}"
    )

    if cost_amount <= budget_amount:
        print("No action: cost within budget.")
        return

    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    projects = billing.projects()
    info = projects.getBillingInfo(name=PROJECT_NAME).execute()

    if info.get("billingEnabled"):
        projects.updateBillingInfo(
            name=PROJECT_NAME,
            body={"billingAccountName": ""},
        ).execute()
        print(f"Billing DISABLED on {PROJECT_NAME}. Re-link via gcloud billing projects link.")
    else:
        print(f"Billing already disabled on {PROJECT_NAME}; no action taken.")
