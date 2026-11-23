#!/usr/bin/env bash
# Run CloudSmith's tests. See tests/README.md.
#
#   scripts/test.sh                    component tests: API, agent and proxy (needs Docker; ~30 s)
#   scripts/test.sh e2e local          end-to-end against the local stack (scripts/local.sh up first)
#   scripts/test.sh e2e local --idle   only the idle-shutdown test, with the API's timeout set to 1 minute
#   scripts/test.sh e2e gcp            end-to-end against the GCP deployment (PROJECT_ID=... like the other scripts)
#
# Anything after these is passed to pytest, e.g.  scripts/test.sh -k proxy -x
set -euo pipefail
cd "$(dirname "$0")/.."

# A virtual environment with the services' dependencies and pytest, rebuilt when the
# requirements change.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
stamp=.venv/.cloudsmith-requirements
wanted=$(cat tests/requirements.txt services/*/requirements.txt | shasum | cut -d' ' -f1)
if [ "$(cat "$stamp" 2>/dev/null)" != "$wanted" ]; then
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r tests/requirements.txt
  echo "$wanted" > "$stamp"
fi
# The Docker SDK doesn't read Docker contexts; point it at the engine the CLI uses.
if [ -z "${DOCKER_HOST:-}" ]; then
  DOCKER_HOST=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
  [ -z "$DOCKER_HOST" ] || export DOCKER_HOST
fi

if [ "${1:-}" != "e2e" ]; then
  exec .venv/bin/pytest "$@"
fi

target="${2:-}"
shift 2 || { echo "usage: $0 e2e local|gcp [--idle] [pytest args]" >&2; exit 1; }
export CLOUDSMITH_TARGET="$target"

case "$target" in
  local)
    export CLOUDSMITH_URL=http://localhost:8000 CLOUDSMITH_WORKER_TIMEOUT=30
    curl -sf "$CLOUDSMITH_URL/healthz" >/dev/null || { echo "The local stack isn't running: scripts/local.sh up" >&2; exit 1; }
    if [ "${1:-}" = "--idle" ]; then
      shift
      running=$(docker ps -q --filter label=cloudsmith.workspace | wc -l | tr -d ' ')
      if [ "$running" != 0 ]; then
        echo "$running workspace(s) are running. With a 1-minute idle timeout the API would stop them too;" >&2
        echo "stop them first." >&2
        exit 1
      fi
      echo "Restarting the API with a 1-minute idle timeout (put back at the end)..."
      IDLE_TIMEOUT_MINUTES=1 docker compose up -d --wait api
      trap 'echo "Restoring the API idle timeout..."; docker compose up -d --wait api' EXIT
      CLOUDSMITH_IDLE_MINUTES=1 .venv/bin/pytest tests/e2e -k unused_workspace "$@"
      exit
    fi
    ;;
  gcp)
    source scripts/common.sh
    CLOUDSMITH_URL=$(gcloud run services describe "$API_SERVICE" --region "$REGION" --format 'value(status.url)')
    export CLOUDSMITH_URL CLOUDSMITH_PROJECT="$PROJECT_ID" CLOUDSMITH_ZONE="$ZONE" CLOUDSMITH_WORKER_TIMEOUT=45
    export CLOUDSMITH_WORKER_COUNT="$WORKER_COUNT"
    echo "Testing $CLOUDSMITH_URL"
    ;;
  *)
    echo "usage: $0 e2e local|gcp [--idle] [pytest args]" >&2
    exit 1
    ;;
esac

.venv/bin/pytest tests/e2e "$@"
