#!/usr/bin/env bash
# Deployment helper for the Cloud Run market-breadth pipeline.
#
# Run this AFTER the GitHub Actions "Build & Push Docker Image" workflow finishes
# (it's triggered automatically by every push to master). The workflow rebuilds
# the Docker image and updates the Cloud Run Job's image tag, but it does NOT
# touch timeout/memory/concurrency settings — those need a one-time bump for
# the new full-pipeline workload (which takes 5-15 min vs. the old 30-sec job).
#
# You can re-run this script safely; all commands are idempotent.

set -euo pipefail

PROJECT="project-feb6df0e-9749-4925-b4e"
REGION="asia-southeast1"
JOB="market-breadth-job"
BUCKET="vn-market-breadth"

echo "== 1) Pre-warm GCS cache from local cache/ =="
echo "   (saves ~12 min on the first cloud run by avoiding 657-ticker re-download)"
if [[ -d "./cache" ]]; then
  gsutil -m -q rsync -r ./cache "gs://${BUCKET}/cache"
  echo "   Cache pre-warm complete."
else
  echo "   WARNING: ./cache not found locally — skipping pre-warm."
fi

echo
echo "== 2) Bump Cloud Run Job timeout (15 min) and memory (2 GiB) =="
gcloud run jobs update "${JOB}" \
  --region "${REGION}" \
  --project "${PROJECT}" \
  --task-timeout=15m \
  --memory=2Gi \
  --cpu=2 \
  --max-retries=1

echo
echo "== 3) Verify the schedule is 15:00 ICT (08:00 UTC) Mon-Fri =="
echo "    Check Cloud Scheduler manually:"
echo "    gcloud scheduler jobs list --project=${PROJECT} --location=${REGION}"
echo "    (Look for the job that targets ${JOB}; cron should be '0 8 * * 1-5')"

echo
echo "== 4) Trigger one manual run NOW to validate end-to-end =="
echo "    (Run this only after step 2 succeeded)"
echo
echo "    gcloud run jobs execute ${JOB} \\"
echo "      --region ${REGION} --project ${PROJECT} --wait"
echo
echo "    Then visit: https://storage.googleapis.com/${BUCKET}/index.html"
echo
echo "== 5) Tail logs from the manual run =="
echo "    gcloud logging read \\"
echo "      'resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB}\"' \\"
echo "      --project ${PROJECT} --limit 200 --order desc"
echo
echo "Done with automated steps. Steps 4-5 are commented suggestions to run by hand."
