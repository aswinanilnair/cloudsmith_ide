#!/usr/bin/env bash
# Create the GCP resources CloudSmith needs. Run once; re-running skips what already exists.
#
#   scripts/setup.sh          (then scripts/deploy.sh)
#   WORKER_COUNT=3 scripts/setup.sh    adds a third worker to an existing setup
#
# Creates: Artifact Registry repo, Pub/Sub topic + one filtered subscription per worker,
# a Cloud Storage bucket for workspace archives, Cloud SQL (PostgreSQL), two service
# accounts, a firewall rule for port 80, WORKER_COUNT worker VMs with static IPs (sending
# their logs and metrics to Cloud Logging and Cloud Monitoring), two log-based metrics and
# the CloudSmith dashboard in Cloud Monitoring.
source "$(dirname "$0")/common.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ ! -f "$SECRETS_FILE" ]; then
  step "Generating secrets in $SECRETS_FILE (keep this file; deploy.sh needs it)"
  umask 077
  cat > "$SECRETS_FILE" <<EOF
DB_PASSWORD=$(openssl rand -hex 16)
TOKEN_SECRET=$(openssl rand -hex 32)
INTERNAL_TOKEN=$(openssl rand -hex 32)
EOF
  set -a; source "$SECRETS_FILE"; set +a
fi

step "Enabling APIs (project $PROJECT_ID)"
gcloud services enable run.googleapis.com sqladmin.googleapis.com pubsub.googleapis.com \
  artifactregistry.googleapis.com compute.googleapis.com storage.googleapis.com \
  logging.googleapis.com monitoring.googleapis.com

step "Artifact Registry repository"
exists gcloud artifacts repositories describe "$REPO" --location "$REGION" ||
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"

step "Pub/Sub topic and one subscription per worker"
exists gcloud pubsub topics describe "$TOPIC" || gcloud pubsub topics create "$TOPIC"
for i in $(worker_ids); do
  sub=$(worker_sub "$i")
  exists gcloud pubsub subscriptions describe "$sub" ||
    gcloud pubsub subscriptions create "$sub" --topic "$TOPIC" --enable-message-ordering --ack-deadline 60 \
      --message-filter "attributes.worker = \"worker-$i\""
  # Back off between redeliveries of a nacked job. Also applies to an existing subscription.
  gcloud pubsub subscriptions update "$sub" --min-retry-delay 10s --max-retry-delay 600s
done

step "Service accounts"
exists gcloud iam service-accounts describe "$API_SA" ||
  gcloud iam service-accounts create cloudsmith-api --display-name "CloudSmith API"
exists gcloud iam service-accounts describe "$WORKER_SA" ||
  gcloud iam service-accounts create cloudsmith-worker --display-name "CloudSmith worker VM"
for role in $API_ROLES; do
  retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$API_SA" --role "$role" --condition None
done
for role in $WORKER_ROLES; do
  retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$WORKER_SA" --role "$role" --condition None
done

step "Cloud Storage bucket for workspace archives (gs://$BUCKET)"
exists gcloud storage buckets describe "gs://$BUCKET" ||
  gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access
# Versioning keeps the previous archive when a new one replaces it; old versions are
# deleted after 7 days.
cat > "$TMP/lifecycle.json" <<'EOF'
{"rule": [{"action": {"type": "Delete"}, "condition": {"isLive": false, "daysSinceNoncurrentTime": 7}}]}
EOF
gcloud storage buckets update "gs://$BUCKET" --versioning --lifecycle-file "$TMP/lifecycle.json"
# Workers read and write archives in this bucket only.
retry gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$WORKER_SA" --role roles/storage.objectAdmin

step "Cloud SQL for PostgreSQL"
exists gcloud sql instances describe "$SQL_INSTANCE" ||
  gcloud sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
    --tier db-f1-micro --region "$REGION" --storage-size 10
exists gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" ||
  gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"
if gcloud sql users list --instance "$SQL_INSTANCE" --format 'value(name)' | grep -qx "$DB_USER"; then
  gcloud sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD"
else
  gcloud sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD"
fi

step "Firewall rule (port 80 to the worker VMs)"
exists gcloud compute firewall-rules describe "$FIREWALL_RULE" ||
  gcloud compute firewall-rules create "$FIREWALL_RULE" --network default --allow tcp:80 \
    --target-tags "$WORKER_TAG" --source-ranges 0.0.0.0/0

for i in $(worker_ids); do
  vm=$(worker_vm "$i"); ip_name=$(worker_ip_name "$i")
  step "Worker VM $vm (Container-Optimized OS, Docker preinstalled) and its static IP"
  exists gcloud compute addresses describe "$ip_name" --region "$REGION" ||
    gcloud compute addresses create "$ip_name" --region "$REGION"
  ip=$(gcloud compute addresses describe "$ip_name" --region "$REGION" --format 'value(address)')
  # Container-Optimized OS sends the containers' logs to Cloud Logging and the VM's memory
  # and disk metrics to Cloud Monitoring when these metadata keys are set.
  observability="google-logging-enabled=true,google-monitoring-enabled=true"
  if exists gcloud compute instances describe "$vm" --zone "$ZONE"; then
    gcloud compute instances add-metadata "$vm" --zone "$ZONE" --metadata "$observability"
  else
    gcloud compute instances create "$vm" --zone "$ZONE" \
      --machine-type "$WORKER_MACHINE_TYPE" \
      --image-family cos-stable --image-project cos-cloud --boot-disk-size "${WORKER_DISK_GB}GB" \
      --address "$ip" --tags "$WORKER_TAG" \
      --service-account "$WORKER_SA" --scopes cloud-platform \
      --metadata "$observability" \
      --metadata-from-file startup-script="$ROOT/scripts/worker-startup.sh"
  fi
done

step "Log-based metrics and the $DASHBOARD_NAME dashboard (Cloud Monitoring)"
for entry in $LOG_METRICS; do
  name=${entry%%:*}; file="$ROOT/monitoring/${entry#*:}"
  if exists gcloud logging metrics describe "$name"; then
    gcloud logging metrics update "$name" --config-from-file "$file"
  else
    gcloud logging metrics create "$name" --config-from-file "$file"
  fi
done
# Replace the dashboard, so edits to monitoring/dashboard.json take effect on a re-run.
gcloud monitoring dashboards list --filter "displayName=\"$DASHBOARD_NAME\"" --format 'value(name)' |
  while read -r dashboard; do gcloud monitoring dashboards delete "$dashboard" --quiet; done
gcloud monitoring dashboards create --config-from-file "$ROOT/monitoring/dashboard.json"

echo
echo "Setup done. Next: scripts/deploy.sh"
