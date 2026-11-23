#!/usr/bin/env bash
# Build the images, deploy the API to Cloud Run, and (re)start the agent and proxy on every
# worker VM. Run after scripts/setup.sh, and again after every code change.
#
#   scripts/deploy.sh
#
# Needs Docker with buildx (images are built for linux/amd64).
source "$(dirname "$0")/common.sh"
[ -n "${DB_PASSWORD:-}" ] || { echo "Missing $SECRETS_FILE; run scripts/setup.sh first" >&2; exit 1; }

TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

step "Building and pushing images to $REGISTRY (tag $TAG)"
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
# Builds an image for linux/amd64 and pushes it to Artifact Registry.
build() {  # build <image name> <directory>
  docker buildx build --platform linux/amd64 -t "$REGISTRY/$1:$TAG" "$ROOT/$2" --push
}
build api services/api
build agent services/agent
build proxy services/proxy
build workspace images/workspace

SQL_CONNECTION=$(gcloud sql instances describe "$SQL_INSTANCE" --format 'value(connectionName)')

step "Deploying the API to Cloud Run"
cat > "$TMP/api-env.yaml" <<EOF
DATABASE_URL: "postgresql://$DB_USER:$DB_PASSWORD@/$DB_NAME?host=/cloudsql/$SQL_CONNECTION"
GCP_PROJECT: "$PROJECT_ID"
PUBSUB_TOPIC: "$TOPIC"
TOKEN_SECRET: "$TOKEN_SECRET"
INTERNAL_TOKEN: "$INTERNAL_TOKEN"
ALLOW_SIGNUP: "$ALLOW_SIGNUP"
MAX_WORKSPACES_PER_USER: "$MAX_WORKSPACES_PER_USER"
IDLE_TIMEOUT_MINUTES: "$IDLE_TIMEOUT_MINUTES"
EOF
gcloud run deploy "$API_SERVICE" --image "$REGISTRY/api:$TAG" --region "$REGION" \
  --service-account "$API_SA" --add-cloudsql-instances "$SQL_CONNECTION" \
  --env-vars-file "$TMP/api-env.yaml" --allow-unauthenticated --quiet
API_URL=$(gcloud run services describe "$API_SERVICE" --region "$REGION" --format 'value(status.url)')

for i in $(worker_ids); do
  vm=$(worker_vm "$i")
  ip=$(gcloud compute addresses describe "$(worker_ip_name "$i")" --region "$REGION" --format 'value(address)')
  step "Updating worker VM $vm ($ip)"
  # worker-startup.sh reads this from the VM's metadata and passes it to the containers.
  # The agent registers the worker with the API through its heartbeats.
  cat > "$TMP/worker-$i.env" <<EOF
WORKER_ID=worker-$i
WORKER_VM=$vm
WORKSPACE_URL=http://ws-{id}.$ip.sslip.io/
WORKER_CAPACITY=$WORKER_CAPACITY
GCP_PROJECT=$PROJECT_ID
PUBSUB_TOPIC=$TOPIC
PUBSUB_SUBSCRIPTION=$(worker_sub "$i")
GCS_BUCKET=$BUCKET
API_URL=$API_URL
INTERNAL_TOKEN=$INTERNAL_TOKEN
TOKEN_SECRET=$TOKEN_SECRET
REGISTRY_HOST=$REGION-docker.pkg.dev
AGENT_IMAGE=$REGISTRY/agent:$TAG
PROXY_IMAGE=$REGISTRY/proxy:$TAG
WORKSPACE_IMAGE=$REGISTRY/workspace:$TAG
PROXY_CONTAINER=cloudsmith-proxy
PROXY_ACTIVITY_URL=http://127.0.0.1/_cloudsmith/activity
WS_CPU_LIMIT=$WS_CPU_LIMIT
WS_MEM_LIMIT_MB=$WS_MEM_LIMIT_MB
EOF
  gcloud compute instances add-metadata "$vm" --zone "$ZONE" \
    --metadata-from-file cloudsmith-env="$TMP/worker-$i.env",startup-script="$ROOT/scripts/worker-startup.sh"
  gcloud compute ssh "$vm" --zone "$ZONE" --command 'sudo google_metadata_script_runner startup'
done

echo
echo "Deployed. Dashboard: $API_URL"
echo "Workspaces are served from http://ws-<id>.<worker-ip>.sslip.io/ on $WORKER_COUNT worker(s)."
echo "Monitoring: https://console.cloud.google.com/monitoring/dashboards?project=$PROJECT_ID (the $DASHBOARD_NAME dashboard)"
echo "Logs:       https://console.cloud.google.com/logs/query?project=$PROJECT_ID (search for a workspace ID to follow it)"
