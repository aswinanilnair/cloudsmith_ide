#!/usr/bin/env bash
# Run CloudSmith locally with Docker Compose.
#
#   scripts/local.sh up      build everything and start it (dashboard: http://localhost:8000)
#   scripts/local.sh down    stop the services; workspaces and data are kept
#   scripts/local.sh clean   stop everything and delete all workspaces, accounts and data
#   scripts/local.sh logs    follow the service logs
set -euo pipefail
cd "$(dirname "$0")/.."

# Removes the workspace containers, networks and volumes created by the agents.
remove_labeled() {
  local ids
  ids=$(docker ps -aq --filter label=cloudsmith.workspace);         [ -z "$ids" ] || docker rm -f $ids >/dev/null
  ids=$(docker network ls -q --filter label=cloudsmith.workspace);  [ -z "$ids" ] || docker network rm $ids >/dev/null
  ids=$(docker volume ls -q --filter label=cloudsmith.workspace);   [ -z "$ids" ] || docker volume rm -f $ids >/dev/null
}

case "${1:-up}" in
  up)
    docker build -t cloudsmith/workspace:latest images/workspace
    docker compose up -d --build --remove-orphans
    echo
    echo "Dashboard: http://localhost:8000   (workspaces open at http://ws-<id>.localhost:8080 or :8081,"
    echo "depending on which of the two workers runs them)"
    ;;
  down)
    docker compose down
    echo "Running workspace containers keep running. 'scripts/local.sh clean' removes them."
    ;;
  clean)
    docker compose down -v
    remove_labeled
    echo "Removed all CloudSmith services, workspaces and data."
    ;;
  logs)
    docker compose logs -f --tail=50 api agent-1 agent-2 proxy-1 proxy-2
    ;;
  *)
    echo "usage: $0 up|down|clean|logs" >&2
    exit 1
    ;;
esac
