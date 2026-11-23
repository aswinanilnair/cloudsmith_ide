# Settings shared by setup.sh, deploy.sh and teardown.sh. Every value can be overridden
# with an environment variable, e.g.  REGION=us-east1 scripts/setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT_ID" ] || { echo "Set PROJECT_ID, or run: gcloud config set project <id>" >&2; exit 1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"  # every gcloud command below uses this project

REGION="${REGION:-us-central1}"
ZONE="${ZONE:-$REGION-a}"

# Worker VMs and workspace sizing
WORKER_COUNT="${WORKER_COUNT:-2}"
WORKER_MACHINE_TYPE="${WORKER_MACHINE_TYPE:-e2-standard-2}"
WORKER_DISK_GB="${WORKER_DISK_GB:-50}"
WORKER_CAPACITY="${WORKER_CAPACITY:-4}"   # workspaces per worker (8 GB fits up to ~6 at 1 GB each)
WS_CPU_LIMIT="${WS_CPU_LIMIT:-1.0}"
WS_MEM_LIMIT_MB="${WS_MEM_LIMIT_MB:-1024}"

# App settings
ALLOW_SIGNUP="${ALLOW_SIGNUP:-true}"
MAX_WORKSPACES_PER_USER="${MAX_WORKSPACES_PER_USER:-3}"
IDLE_TIMEOUT_MINUTES="${IDLE_TIMEOUT_MINUTES:-30}"   # 0 turns idle shutdown off

# Resource names
REPO=cloudsmith
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO"
TOPIC=workspace-jobs
BUCKET="${BUCKET:-$PROJECT_ID-cloudsmith-workspaces}"
SQL_INSTANCE=cloudsmith-db
DB_NAME=cloudsmith
DB_USER=cloudsmith
API_SERVICE=cloudsmith-api
WORKER_TAG=cloudsmith-worker

# Worker i (1..WORKER_COUNT) is the VM cloudsmith-worker-i with static IP cloudsmith-worker-i-ip.
# Its agent runs as WORKER_ID worker-i and reads the subscription workspace-jobs-worker-i,
# which only receives jobs whose "worker" attribute is worker-i.
worker_ids()     { seq 1 "$WORKER_COUNT"; }
worker_vm()      { echo "cloudsmith-worker-$1"; }
worker_ip_name() { echo "cloudsmith-worker-$1-ip"; }
worker_sub()     { echo "$TOPIC-worker-$1"; }
FIREWALL_RULE=cloudsmith-allow-http
API_SA="cloudsmith-api@$PROJECT_ID.iam.gserviceaccount.com"
WORKER_SA="cloudsmith-worker@$PROJECT_ID.iam.gserviceaccount.com"
API_ROLES="roles/pubsub.publisher roles/cloudsql.client"
WORKER_ROLES="roles/pubsub.subscriber roles/artifactregistry.reader roles/logging.logWriter roles/monitoring.metricWriter"

# Observability (monitoring/): log-based metrics as <name>:<config file>, and the dashboard.
LOG_METRICS="cloudsmith_workspaces_per_worker:metric-workspaces-per-worker.yaml cloudsmith_failed_jobs:metric-failed-jobs.yaml"
DASHBOARD_NAME=CloudSmith

# Secrets (DB_PASSWORD, TOKEN_SECRET, INTERNAL_TOKEN) live in this file, created by setup.sh.
SECRETS_FILE="$ROOT/.env.gcp"
if [ -f "$SECRETS_FILE" ]; then
  set -a; source "$SECRETS_FILE"; set +a
fi

# Prints a step heading.
step() { printf '\n==> %s\n' "$*"; }
# Whether a command succeeds (used to skip resources that already exist).
exists() { "$@" >/dev/null 2>&1; }
# Retries a command a few times: a new service account can take a few seconds to become visible to IAM.
retry() {  # retry <command>...
  for attempt in 1 2 3 4 5 6; do
    "$@" >/dev/null && return 0
    [ "$attempt" = 6 ] && return 1
    sleep 10
  done
}
