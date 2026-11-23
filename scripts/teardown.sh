#!/usr/bin/env bash
# Delete every GCP resource that setup.sh and deploy.sh created, including the database,
# all worker VMs and every workspace archive. Keeps .env.gcp so a later setup.sh reuses
# the same secrets.
#
#   scripts/teardown.sh
source "$(dirname "$0")/common.sh"

read -r -p "Delete all CloudSmith resources in project $PROJECT_ID, including the database and every workspace? [y/N] " answer
[ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "Cancelled."; exit 1; }

# Runs a delete command, and keeps going if the resource is already gone.
try() { "$@" --quiet || echo "   (skipped: $*)"; }

step "Cloud Run service";   try gcloud run services delete "$API_SERVICE" --region "$REGION"

# Workers are found by name, so this also removes any left over from a larger WORKER_COUNT.
step "Worker VMs"
gcloud compute instances list --filter "name ~ ^cloudsmith-worker-" --format 'value(name,zone.basename())' |
  while read -r name zone; do try gcloud compute instances delete "$name" --zone "$zone"; done
step "Static IPs"
gcloud compute addresses list --filter "name ~ ^cloudsmith-worker-" --format 'value(name,region.basename())' |
  while read -r name region; do try gcloud compute addresses delete "$name" --region "$region"; done

step "Firewall rule";       try gcloud compute firewall-rules delete "$FIREWALL_RULE"
step "Cloud SQL instance";  try gcloud sql instances delete "$SQL_INSTANCE"
step "Pub/Sub"
gcloud pubsub subscriptions list --filter "name ~ /subscriptions/$TOPIC-worker-" --format 'value(name)' |
  while read -r sub; do try gcloud pubsub subscriptions delete "$sub"; done
try gcloud pubsub topics delete "$TOPIC"
step "Cloud Storage bucket and all workspace archives"
try gcloud storage rm --recursive --all-versions "gs://$BUCKET"
step "Artifact Registry";   try gcloud artifacts repositories delete "$REPO" --location "$REGION"
step "Dashboard and log-based metrics"
gcloud monitoring dashboards list --filter "displayName=\"$DASHBOARD_NAME\"" --format 'value(name)' |
  while read -r dashboard; do try gcloud monitoring dashboards delete "$dashboard"; done
for entry in $LOG_METRICS; do try gcloud logging metrics delete "${entry%%:*}"; done

step "Service accounts"
for role in $API_ROLES; do
  try gcloud projects remove-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$API_SA" --role "$role" --condition None
done
for role in $WORKER_ROLES; do
  try gcloud projects remove-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$WORKER_SA" --role "$role" --condition None
done
try gcloud iam service-accounts delete "$API_SA"
try gcloud iam service-accounts delete "$WORKER_SA"

echo
echo "Teardown done. The enabled APIs and $SECRETS_FILE were left in place."
