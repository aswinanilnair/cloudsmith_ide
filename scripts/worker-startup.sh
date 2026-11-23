#!/bin/bash
# Startup script of each worker VM (Container-Optimized OS). Runs at every boot, and
# deploy.sh re-runs it after updating the VM's metadata. It pulls the images and
# (re)starts the proxy and the agent. Workspace containers are left as they are. The
# configuration, including this worker's WORKER_ID, comes from the cloudsmith-env metadata.
set -euo pipefail

STATE_DIR=/var/lib/cloudsmith
ENV_FILE=$STATE_DIR/env
METADATA=http://metadata.google.internal/computeMetadata/v1/instance/attributes
mkdir -p "$STATE_DIR"

if ! curl -sf -H 'Metadata-Flavor: Google' "$METADATA/cloudsmith-env" -o "$ENV_FILE"; then
  echo "cloudsmith: no configuration in metadata yet; run scripts/deploy.sh"
  exit 0
fi
set -a; source "$ENV_FILE"; set +a

# Let Docker pull from Artifact Registry with the VM's service account.
export HOME=$STATE_DIR
docker-credential-gcr configure-docker --registries="$REGISTRY_HOST"
docker pull "$WORKSPACE_IMAGE"
docker pull "$PROXY_IMAGE"
docker pull "$AGENT_IMAGE"

# Workspaces must not reach the metadata server: it hands out this VM's credentials.
# (The agent uses host networking, which these rules do not affect.) The same address is
# also the VM's DNS resolver, and Docker forwards workspace DNS lookups to it from inside
# the workspace's network, so DNS (port 53) is let through; -I puts those rules first.
iptables -C DOCKER-USER -d 169.254.169.254 -j DROP 2>/dev/null ||
  iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
for proto in udp tcp; do
  iptables -C DOCKER-USER -d 169.254.169.254 -p "$proto" --dport 53 -j ACCEPT 2>/dev/null ||
    iptables -I DOCKER-USER -d 169.254.169.254 -p "$proto" --dport 53 -j ACCEPT
done

docker rm -f cloudsmith-proxy cloudsmith-agent >/dev/null 2>&1 || true
docker run -d --name cloudsmith-proxy --restart unless-stopped \
  -p 80:8080 --env-file "$ENV_FILE" "$PROXY_IMAGE"
docker run -d --name cloudsmith-agent --restart unless-stopped \
  --network host -v /var/run/docker.sock:/var/run/docker.sock --env-file "$ENV_FILE" "$AGENT_IMAGE"

echo "cloudsmith: proxy and agent started"
