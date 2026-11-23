"""Agent test setup: real Docker, a throwaway fake-gcs-server for Cloud Storage, a tiny
workspace image (Alpine with the same `coder` user as the real one, so the tests run in
seconds), and a stand-in proxy container. The agent runs as worker "test-worker", so a
local CloudSmith stack running at the same time is left alone."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import helpers  # noqa: E402

if not helpers.docker_available():
    collect_ignore_glob = ["test_*.py"]
else:
    GCS_PORT = helpers.free_port()
    GCS_CONTAINER = "cloudsmith-test-gcs"
    PROXY_CONTAINER = "cloudsmith-test-proxy"
    TEST_IMAGE = "cloudsmith/test-workspace:latest"

    helpers.set_docker_host()
    os.environ.update({  # the agent reads its settings when it is imported
        "WORKER_ID": "test-worker",
        "GCP_PROJECT": "test-project",
        "GCS_BUCKET": "cloudsmith-test-workspaces",
        "STORAGE_EMULATOR_HOST": f"http://127.0.0.1:{GCS_PORT}",
        "WORKSPACE_IMAGE": TEST_IMAGE,
        "WORKSPACE_URL": "http://ws-{id}.worker.test/",
        "PROXY_CONTAINER": PROXY_CONTAINER,
        "API_URL": "http://127.0.0.1:1",  # the tests replace report()
        "INTERNAL_TOKEN": "test-internal-token",
        "WS_CPU_LIMIT": "1.0",
        "WS_MEM_LIMIT_MB": "1024",
    })
    sys.path.insert(0, str(helpers.ROOT / "services" / "agent"))
    import agent  # noqa: E402

TEST_DOCKERFILE = """
FROM alpine:3.20
RUN adduser -D -u 1000 coder && mkdir -p /home/coder/project && chown coder:coder /home/coder/project
USER coder
WORKDIR /home/coder/project
# Exit promptly on docker stop, as code-server does.
CMD ["sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"]
"""


# Builds the test workspace image and starts the stand-ins the agent needs, once per session.
@pytest.fixture(scope="session")
def docker_env():
    # The docker CLI, not the SDK: the SDK loads every registry credential helper in
    # ~/.docker/config.json for a build, and fails if any of them does (e.g. an expired gcloud login).
    subprocess.run(["docker", "build", "-q", "-t", TEST_IMAGE, "-"], input=TEST_DOCKERFILE.encode(),
                   check=True, capture_output=True)
    helpers.run_container(PROXY_CONTAINER, "--init", "alpine:3.20", "sleep", "infinity")
    helpers.run_container(GCS_CONTAINER, "-p", f"127.0.0.1:{GCS_PORT}:4443", "fsouza/fake-gcs-server:1.52.2",
                          "-scheme", "http", "-port", "4443", "-external-url", f"http://127.0.0.1:{GCS_PORT}")
    helpers.wait_until(lambda: helpers.http_ok(f"http://127.0.0.1:{GCS_PORT}/storage/v1/b"), 60, "fake-gcs-server")
    agent.gcs.create_bucket(agent.GCS_BUCKET)
    yield
    helpers.remove_container(PROXY_CONTAINER)
    helpers.remove_container(GCS_CONTAINER)


# A new workspace ID; anything left for it is removed after the test.
@pytest.fixture
def ws_id(docker_env):
    ws_id = str(uuid.uuid4())
    yield ws_id
    agent.remove_workspace(ws_id)
    blob = agent.archive_blob(ws_id)
    if blob.exists():
        blob.delete()
