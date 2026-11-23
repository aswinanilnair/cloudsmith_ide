"""End-to-end test setup. The tests drive a running CloudSmith the way a user would: through
the public API and the workspace links, with a cookie-keeping HTTP session as the browser.

Two things a user does in the editor can't be done over plain HTTP, so the tests reach into
the workspace container for them: writing and reading files, and starting an app on a port.
They also stop and start a worker's agent to simulate losing a worker.

Settings (scripts/test.sh sets them):
  CLOUDSMITH_URL             the dashboard, e.g. http://localhost:8000 (no URL: tests skipped)
  CLOUDSMITH_TARGET          local (Docker Compose) or gcp
  CLOUDSMITH_PROJECT/ZONE    for gcp: where the worker VMs are
  CLOUDSMITH_WORKER_TIMEOUT  the API's WORKER_TIMEOUT_SECONDS (30 local, 45 on GCP)
  CLOUDSMITH_IDLE_MINUTES    set only to run the idle-shutdown test
"""
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
URL = os.getenv("CLOUDSMITH_URL", "").rstrip("/")
TARGET = os.getenv("CLOUDSMITH_TARGET", "local")
WORKER_TIMEOUT = int(os.getenv("CLOUDSMITH_WORKER_TIMEOUT", "45"))
TIMEOUT = int(os.getenv("CLOUDSMITH_TIMEOUT", "300"))  # for one workspace action to finish


# Skips every end-to-end test when no CloudSmith URL is set.
def pytest_collection_modifyitems(config, items):
    if not URL:
        skip = pytest.mark.skip(reason="CLOUDSMITH_URL is not set; run scripts/test.sh e2e local|gcp")
        for item in items:
            item.add_marker(skip)


# ------------------------------------------------------------------------- workers


class LocalWorkers:
    """The local Docker Compose stack: workers share one Docker engine."""

    # Runs a shell command inside a workspace container.
    def exec(self, worker: str, ws_id: str, command: str, detach: bool = False) -> str:
        args = ["docker", "exec", *(["-d"] if detach else []), f"ws-{ws_id}", "sh", "-c", command]
        return run(args)

    # Counts the running workspace containers (all local workers share one Docker engine).
    def running_workspaces(self) -> int:
        return len(run(["docker", "ps", "-q", "--filter", "label=cloudsmith.workspace"]).split())

    # Whether this workspace's container is running on the given worker.
    def running_on(self, worker: str, ws_id: str) -> bool:
        return bool(run(["docker", "ps", "-q", "--filter", f"name=^ws-{ws_id}$",
                         "--filter", f"label=cloudsmith.worker={worker}"]).strip())

    # Stops a worker's agent service.
    def stop_agent(self, worker: str) -> None:
        run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "stop", self.agent(worker)])

    # Starts a worker's agent service again.
    def start_agent(self, worker: str) -> None:
        run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "start", self.agent(worker)])

    # Maps a worker ID to its Compose service name, e.g. worker-1 -> agent-1.
    @staticmethod
    def agent(worker: str) -> str:
        return worker.replace("worker-", "agent-")  # worker-1 -> the agent-1 service


class GcpWorkers:
    """Worker VMs, reached with gcloud compute ssh."""

    # Reads the GCP project and zone of the worker VMs from the environment.
    def __init__(self):
        self.project = os.environ["CLOUDSMITH_PROJECT"]
        self.zone = os.environ["CLOUDSMITH_ZONE"]

    # Runs a command on a worker VM over SSH.
    def ssh(self, worker: str, command: str) -> str:
        vm = f"cloudsmith-{worker}"  # worker-1 -> cloudsmith-worker-1
        return run(["gcloud", "compute", "ssh", vm, "--zone", self.zone, "--project", self.project,
                    "--quiet", "--command", command])

    # Runs a shell command inside a workspace container on a worker VM.
    def exec(self, worker: str, ws_id: str, command: str, detach: bool = False) -> str:
        return self.ssh(worker, f"sudo docker exec {'-d ' if detach else ''}ws-{ws_id} sh -c {shlex.quote(command)}")

    # Counts the running workspace containers across all worker VMs.
    def running_workspaces(self) -> int:
        count = int(os.getenv("CLOUDSMITH_WORKER_COUNT", "2"))
        return sum(len(self.ssh(f"worker-{i}", "sudo docker ps -q --filter label=cloudsmith.workspace").split())
                   for i in range(1, count + 1))

    # Whether this workspace's container is running on the given worker VM.
    def running_on(self, worker: str, ws_id: str) -> bool:
        return bool(self.ssh(worker, f"sudo docker ps -q --filter name=^ws-{ws_id}$").strip())

    # Stops the agent container on a worker VM.
    def stop_agent(self, worker: str) -> None:
        self.ssh(worker, "sudo docker stop cloudsmith-agent")

    # Starts the agent container on a worker VM again.
    def start_agent(self, worker: str) -> None:
        self.ssh(worker, "sudo docker start cloudsmith-agent")


# Runs a command and returns its output; raises with the output if it fails.
def run(args: list) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


# The workers of the stack under test: local Docker Compose or GCP VMs.
@pytest.fixture(scope="session")
def workers():
    return GcpWorkers() if TARGET == "gcp" else LocalWorkers()


# Counts the workspaces that were already running before the tests started.
@pytest.fixture(scope="session")
def others_running(workers):
    return workers.running_workspaces()


# Lets a test stop worker agents, and starts them again when the test ends.
@pytest.fixture
def stopped_agents(workers, others_running):
    """stopped_agents.stop("worker-1"): stop a worker's agent until the test ends.

    Skipped while other workspaces run: the API would move them off the stopped worker and
    restart them from their last archive, losing their unsaved changes."""
    if others_running:
        pytest.skip(f"{others_running} workspace(s) not made by these tests are running; stop them first, "
                    "since these tests take workers away")
    class Stopped:
        # Tracks which agents a test has stopped.
        def __init__(self):
            self.workers = []

        # Stops a worker's agent and remembers it.
        def stop(self, worker):
            workers.stop_agent(worker)
            self.workers.append(worker)

        # Starts a stopped worker's agent again.
        def start(self, worker):
            workers.start_agent(worker)
            self.workers.remove(worker)

    stopped = Stopped()
    yield stopped
    for worker in stopped.workers:
        workers.start_agent(worker)


# ---------------------------------------------------------------------------- users


class User:
    """A signed-up user of the dashboard API."""

    # Signs up a new user and keeps their session header.
    def __init__(self, name: str):
        self.name = name
        resp = requests.post(f"{URL}/api/signup", json={"username": name, "password": "e2e-password"}, timeout=30)
        assert resp.status_code == 200, resp.text
        self.headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    # Sends an authenticated request to the API.
    def call(self, method: str, path: str, **kwargs) -> requests.Response:
        return requests.request(method, f"{URL}{path}", headers=self.headers, timeout=60, **kwargs)

    # Creates a workspace and returns it.
    def create(self, name: str = "e2e") -> dict:
        resp = self.call("POST", "/api/workspaces", json={"name": name})
        assert resp.status_code == 202, resp.text
        return resp.json()

    # Returns one of the user's workspaces as the dashboard lists it.
    def workspace(self, ws_id: str) -> dict:
        for ws in self.call("GET", "/api/workspaces").json()["workspaces"]:
            if ws["id"] == ws_id:
                return ws
        raise AssertionError(f"workspace {ws_id} is not on the dashboard")

    # Sends an action (start, stop) for a workspace.
    def action(self, ws_id: str, action: str) -> None:
        resp = self.call("POST", f"/api/workspaces/{ws_id}/{action}")
        assert resp.status_code == 202, resp.text

    # Polls the dashboard until the workspace reaches a state (and an optional check holds).
    def wait(self, ws_id: str, state: str, timeout: float = TIMEOUT, check=lambda ws: True) -> dict:
        deadline, ws = time.time() + timeout, None
        while time.time() < deadline:
            ws = self.workspace(ws_id)
            if ws["state"] == state and check(ws):
                return ws
            if ws["state"] == "FAILED" and state != "FAILED":
                raise AssertionError(f"workspace failed: {ws['error']}")
            time.sleep(2)
        raise AssertionError(f"workspace did not reach {state} in {timeout:.0f} s; last seen: {ws}")

    # Returns the Open link of a workspace.
    def open_link(self, ws_id: str) -> str:
        resp = self.call("GET", f"/api/workspaces/{ws_id}/open")
        assert resp.status_code == 200, resp.text
        return resp.json()["url"]

    # Deletes all of the user's workspaces.
    def delete_all(self) -> None:
        for ws in self.call("GET", "/api/workspaces").json()["workspaces"]:
            self.call("DELETE", f"/api/workspaces/{ws['id']}")


# A new test user whose workspaces are deleted when the test ends.
@pytest.fixture
def user():
    """A new user (e2e-<random>); their workspaces are deleted when the test ends. The account
    itself stays, since the API has no way to delete users."""
    u = User(f"e2e-{uuid.uuid4().hex[:10]}")
    yield u
    u.delete_all()


# A second test user, for tests that need two accounts.
@pytest.fixture
def another_user():
    u = User(f"e2e-{uuid.uuid4().hex[:10]}")
    yield u
    u.delete_all()


# Gives tests the _open_editor helper.
@pytest.fixture
def open_editor():
    return _open_editor


# Follows an Open link like a browser until code-server answers.
def _open_editor(browser: requests.Session, link: str, timeout: float = 120) -> requests.Response:
    """Follow an Open link like a browser, until code-server answers (it needs a few seconds
    after the container starts; until then the proxy shows its 'starting' page)."""
    deadline = time.time() + timeout
    resp = browser.get(link, timeout=30)  # swaps the token for the cookie
    while resp.status_code == 502 and time.time() < deadline:
        time.sleep(2)
        resp = browser.get(resp.url, timeout=30)
    return resp
