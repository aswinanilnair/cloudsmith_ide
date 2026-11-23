"""API test setup: a throwaway PostgreSQL container, and a fake Pub/Sub publisher that
records every job instead of sending it. Each test starts with empty tables."""
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import helpers  # noqa: E402

PG_PORT = helpers.free_port()
PG_CONTAINER = "cloudsmith-test-postgres"

# The API reads its settings when it is imported, so set them first.
os.environ.update({
    "DATABASE_URL": f"postgresql://cloudsmith:cloudsmith@127.0.0.1:{PG_PORT}/cloudsmith",
    "TOKEN_SECRET": "test-token-secret",
    "INTERNAL_TOKEN": "test-internal-token",
    "ALLOW_SIGNUP": "true",
    "MAX_WORKSPACES_PER_USER": "3",
    "WORKER_TIMEOUT_SECONDS": "45",
    "IDLE_TIMEOUT_MINUTES": "30",
    "PUBSUB_EMULATOR_HOST": "127.0.0.1:1",  # never contacted: the tests replace the publisher
})
sys.path.insert(0, str(helpers.ROOT / "services" / "api"))
from app import db, main  # noqa: E402

INTERNAL = {"X-Internal-Token": "test-internal-token"}


class FakePublisher:
    """Stands in for pubsub_v1.PublisherClient. Records each job as a dict with its action,
    workspace_id, ordering_key and worker attribute. Set fail = True to make publishing fail."""

    # Starts with no jobs recorded.
    def __init__(self):
        self.jobs = []
        self.resumed = []
        self.fail = False

    # Topics need no setup in the fake.
    def create_topic(self, name):
        pass

    # Records a published job, or fails if told to.
    def publish(self, topic, data, ordering_key, **attributes):
        future = concurrent.futures.Future()
        if self.fail:
            future.set_exception(RuntimeError("Pub/Sub is unavailable"))
        else:
            self.jobs.append({**json.loads(data), "ordering_key": ordering_key, **attributes})
            future.set_result("message-id")
        return future

    # Records that an ordering key was resumed.
    def resume_publish(self, topic, ordering_key):
        self.resumed.append(ordering_key)


# Runs a throwaway PostgreSQL container for the test session.
@pytest.fixture(scope="session")
def postgres():
    if not helpers.docker_available():
        pytest.skip("Docker is not running")
    helpers.run_container(
        PG_CONTAINER, "-p", f"127.0.0.1:{PG_PORT}:5432",
        "-e", "POSTGRES_USER=cloudsmith", "-e", "POSTGRES_PASSWORD=cloudsmith", "-e", "POSTGRES_DB=cloudsmith",
        "postgres:16-alpine",
    )
    # pg_isready inside the container can succeed during the image's init restart, so check
    # with a real query from the host.
    helpers.wait_until(
        lambda: subprocess.run(["docker", "exec", PG_CONTAINER, "psql", "-h", "127.0.0.1", "-U", "cloudsmith",
                                "-c", "SELECT 1"], capture_output=True).returncode == 0,
        60, "PostgreSQL")
    yield
    db.pool.close()
    helpers.remove_container(PG_CONTAINER)


# Replaces the API's Pub/Sub publisher with the recording fake.
@pytest.fixture(scope="session")
def publisher():
    fake = FakePublisher()
    main.publisher = fake
    return fake


# Starts the API once per session, with fast password hashing.
@pytest.fixture(scope="session")
def _app(postgres, publisher):
    from fastapi.testclient import TestClient
    main.PASSWORD_ITERATIONS = 1000  # hashing is deliberately slow; the tests don't need that
    with TestClient(main.app) as client:  # runs the API's startup: opens the pool, creates the tables
        yield client


# The API test client, with empty tables and a reset publisher for each test.
@pytest.fixture
def client(_app, publisher):
    db.execute("TRUNCATE workspaces, workers, users CASCADE")
    publisher.jobs.clear()
    publisher.resumed.clear()
    publisher.fail = False
    return _app


# ------------------------------------------------------------------------ helpers


# Signs up a user and returns their Authorization header.
@pytest.fixture
def signup(client):
    """signup("alice") -> the Authorization header for a new user."""
    # Signs up and returns the session header.
    def _signup(username: str, password: str = "password123") -> dict:
        resp = client.post("/api/signup", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['token']}"}
    return _signup


# Sends a worker heartbeat and returns the API's reply.
@pytest.fixture
def heartbeat(client):
    """heartbeat("worker-1", capacity=3, activity={...}, vm="vm-1") -> the API's reply."""
    # Posts the heartbeat with the internal token.
    def _heartbeat(worker_id: str, capacity: int = 3, activity: dict = None, vm: str = "") -> dict:
        resp = client.post(
            f"/internal/workers/{worker_id}/heartbeat",
            json={"workspace_url": f"http://ws-{{id}}.{worker_id}.test/", "capacity": capacity,
                  "activity": activity or {}, "vm": vm},
            headers=INTERNAL,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()
    return _heartbeat


# Reports a workspace state as the agent does after a job.
@pytest.fixture
def report(client):
    """report(ws_id, "RUNNING", "worker-1") as the agent does after a job."""
    # Posts the state report with the internal token.
    def _report(ws_id: str, state: str, worker_id: str, error: str = None):
        resp = client.post(f"/internal/workspaces/{ws_id}/state",
                           json={"state": state, "worker_id": worker_id, "error": error}, headers=INTERNAL)
        assert resp.status_code == 204, resp.text
    return _report


# Creates a workspace and returns it as the dashboard sees it.
@pytest.fixture
def create(client):
    """create(headers, "name") -> the new workspace (as the dashboard sees it)."""
    # Posts the create request.
    def _create(headers: dict, name: str = "project") -> dict:
        resp = client.post("/api/workspaces", json={"name": name}, headers=headers)
        assert resp.status_code == 202, resp.text
        return resp.json()
    return _create


# Creates a workspace and reports it RUNNING, as its worker would.
@pytest.fixture
def running(create, report):
    """running(headers) -> a workspace its worker has started."""
    # Creates the workspace and reports it running.
    def _running(headers: dict, name: str = "project") -> dict:
        ws = create(headers, name)
        report(ws["id"], "RUNNING", ws["worker"])
        return ws
    return _running


# Reads a workspace's database row.
@pytest.fixture
def row(client):
    return lambda ws_id: db.fetchone("SELECT * FROM workspaces WHERE id = %s", (ws_id,))


# Runs SQL directly, e.g. to make time pass.
@pytest.fixture
def sql(client):
    return db.execute
