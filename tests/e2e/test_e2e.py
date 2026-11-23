"""End to end, as a user: the success criteria from the proposal (section 2).

  * create a workspace, open it in the browser, and run an app on a port
  * write files, stop it, and find them after it starts again on a different worker
  * when a worker is lost, its workspaces come back on a healthy worker from their archive
  * a worker that comes back does not run jobs that were meant for it before the workspace moved
  * idle workspaces are stopped automatically (run separately: scripts/test.sh e2e local --idle)

Run with scripts/test.sh e2e local|gcp. The worker tests need two workers.
"""
import os
import time
from urllib.parse import urlsplit

import pytest
import requests

pytestmark = pytest.mark.e2e
WORKER_TIMEOUT = int(os.getenv("CLOUDSMITH_WORKER_TIMEOUT", "45"))  # the API's WORKER_TIMEOUT_SECONDS

FILE = "/home/coder/project/e2e.txt"
CONTENT = "written before the move"


# Creates and opens a workspace, runs an app on a port; strangers and other users cannot open it.
def test_create_open_and_use_a_port(user, another_user, workers, open_editor):
    ws = user.create()
    ws = user.wait(ws["id"], "RUNNING")
    link = user.open_link(ws["id"])

    browser = requests.Session()
    editor = open_editor(browser, link)
    assert editor.status_code == 200, editor.text[:500]
    assert "code-server" in editor.text.lower() or "vscode" in editor.text.lower()
    editor_url = urlsplit(editor.url)
    assert editor_url.hostname.startswith(f"ws-{ws['id']}.")

    # Without the cookie (another browser, or a copied URL), the workspace is closed...
    stranger = requests.get(f"{editor_url.scheme}://{editor_url.netloc}/", timeout=30)
    assert stranger.status_code == 401
    # ...and another user can't get a link to it.
    assert another_user.call("GET", f"/api/workspaces/{ws['id']}/open").status_code == 404

    # An app on port 5000, opened at its own host as the Ports tab does.
    workers.exec(ws["worker"], ws["id"], f"echo hello from port 5000 > {FILE}")
    workers.exec(ws["worker"], ws["id"],
                 "python3 -m http.server 5000 --bind 127.0.0.1 --directory /home/coder/project", detach=True)
    port_url = f"{editor_url.scheme}://5000-{editor_url.netloc}/e2e.txt"
    deadline = time.time() + 60
    while True:
        app = browser.get(port_url, timeout=30)  # signs in to the port host through the editor host
        if app.status_code == 200 or time.time() > deadline:
            break
        time.sleep(2)
    assert app.status_code == 200, app.text[:500]
    assert app.text == "hello from port 5000\n"
    assert urlsplit(app.url).hostname.startswith(f"5000-ws-{ws['id']}.")
    assert requests.get(port_url, timeout=30, allow_redirects=False).status_code == 302  # no cookie: sign in first


# Files written in a workspace survive a stop and a start on another worker.
def test_files_survive_stop_and_start_on_another_worker(user, workers, stopped_agents):
    ws = user.wait(user.create()["id"], "RUNNING")
    first = ws["worker"]
    workers.exec(first, ws["id"], f"echo '{CONTENT}' > {FILE}")

    user.action(ws["id"], "stop")
    user.wait(ws["id"], "STOPPED")

    # Take the first worker away, and wait until the API has noticed, so the workspace can
    # only start on another worker.
    stopped_agents.stop(first)
    time.sleep(WORKER_TIMEOUT + 15)

    user.action(ws["id"], "start")
    ws = user.wait(ws["id"], "RUNNING")
    assert ws["worker"] != first
    assert workers.exec(ws["worker"], ws["id"], f"cat {FILE}") == f"{CONTENT}\n"


# When a worker is lost, its workspace is restarted on another worker.
def test_a_lost_workers_workspace_comes_back_on_another_worker(user, workers, stopped_agents):
    ws = user.wait(user.create()["id"], "RUNNING")
    workers.exec(ws["worker"], ws["id"], f"echo '{CONTENT}' > {FILE}")
    user.action(ws["id"], "stop")  # archive the file
    user.wait(ws["id"], "STOPPED")
    user.action(ws["id"], "start")
    ws = user.wait(ws["id"], "RUNNING")
    lost = ws["worker"]

    stopped_agents.stop(lost)  # the worker stops responding while the workspace runs
    ws = user.wait(ws["id"], "RUNNING", timeout=WORKER_TIMEOUT + 300, check=lambda w: w["worker"] != lost)
    assert workers.exec(ws["worker"], ws["id"], f"cat {FILE}") == f"{CONTENT}\n"


# A worker that comes back skips jobs that were reassigned while it was down.
def test_a_returning_worker_does_not_run_old_jobs(user, workers, stopped_agents):
    """A worker dies and, before the API notices, a workspace is started on it. The API then
    restarts the workspace on another worker. When the first worker comes back, the old start
    job is still waiting for it; it must skip that job rather than start a second copy (or,
    with the local stack's shared Docker, take over the healthy copy)."""
    ws = user.wait(user.create()["id"], "RUNNING")
    workers.exec(ws["worker"], ws["id"], f"echo '{CONTENT}' > {FILE}")
    user.action(ws["id"], "stop")
    user.wait(ws["id"], "STOPPED")

    dead = "worker-1"
    stopped_agents.stop(dead)
    placed = user.call("POST", f"/api/workspaces/{ws['id']}/start").json()["worker"]
    if placed != dead:
        pytest.skip(f"the start was placed on {placed}, so no job waited for {dead}; "
                    "this needs the workers to have equal free slots")
    ws = user.wait(ws["id"], "RUNNING", timeout=WORKER_TIMEOUT + 300, check=lambda w: w["worker"] != dead)
    healthy = ws["worker"]

    stopped_agents.start(dead)
    time.sleep(30)  # the returning agent receives the old start job and should skip it
    assert not workers.running_on(dead, ws["id"])
    assert workers.running_on(healthy, ws["id"])
    ws = user.workspace(ws["id"])
    assert ws["state"] == "RUNNING" and ws["worker"] == healthy
    assert workers.exec(healthy, ws["id"], f"cat {FILE}") == f"{CONTENT}\n"


# A workspace nobody uses is stopped after the idle timeout.
@pytest.mark.skipif(not os.getenv("CLOUDSMITH_IDLE_MINUTES"),
                    reason="needs the API's idle timeout shortened; run scripts/test.sh e2e local --idle")
def test_an_unused_workspace_is_stopped(user):
    minutes = int(os.environ["CLOUDSMITH_IDLE_MINUTES"])
    ws = user.wait(user.create()["id"], "RUNNING")
    # Nobody opens it. It should stop within the timeout plus a few heartbeats.
    ws = user.wait(ws["id"], "STOPPED", timeout=minutes * 60 + 120)
    assert ws["error"] == f"Stopped automatically after {minutes} minute{'s' * (minutes != 1)} without use"
