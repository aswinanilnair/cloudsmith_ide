"""The workspace lifecycle: each action moves the workspace to the right state and sends the
right job to the right worker, and the agent's reports are applied only when they fit."""
import jwt
import pytest
from app import main


# --------------------------------------------------------------------------- create


# Create places the workspace on a worker and sends a start job.
def test_create_places_the_workspace_and_sends_a_start_job(client, signup, heartbeat, publisher, row):
    heartbeat("worker-1")
    alice = signup("alice")
    resp = client.post("/api/workspaces", json={"name": "  my project "}, headers=alice)
    assert resp.status_code == 202
    ws = resp.json()
    assert ws["name"] == "my project" and ws["state"] == "PENDING" and ws["worker"] == "worker-1"
    assert publisher.jobs == [{"action": "start", "workspace_id": ws["id"], "ordering_key": ws["id"],
                               "worker": "worker-1"}]
    assert row(ws["id"])["state"] == "PENDING"


# Create rejects invalid names.
@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_create_rejects_bad_names(client, signup, heartbeat, publisher, name):
    heartbeat("worker-1")
    resp = client.post("/api/workspaces", json={"name": name}, headers=signup("alice"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Name must be 1-40 characters"
    assert publisher.jobs == []


# Each user has a workspace quota.
def test_each_user_has_a_workspace_quota(client, signup, heartbeat, create):
    heartbeat("worker-1", capacity=10)
    alice = signup("alice")
    for i in range(3):
        create(alice, f"ws-{i}")
    resp = client.post("/api/workspaces", json={"name": "one too many"}, headers=alice)
    assert resp.status_code == 429
    assert resp.json()["detail"] == "You can have at most 3 workspaces"
    create(signup("bob"), "bob-ws")  # the quota is per user


# Create fails cleanly when no worker is live.
def test_create_fails_cleanly_without_a_live_worker(client, signup, publisher):
    alice = signup("alice")
    resp = client.post("/api/workspaces", json={"name": "ws"}, headers=alice)
    assert resp.status_code == 503
    assert resp.json()["detail"] == main.NO_CAPACITY
    assert client.get("/api/workspaces", headers=alice).json()["workspaces"] == []  # nothing half-created
    assert publisher.jobs == []


# ---------------------------------------------------------------------- stop / start


# A workspace can be stopped and started again.
def test_stop_then_start(client, signup, heartbeat, running, report, publisher, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    assert row(ws["id"])["last_active"] is not None  # starting counts as use, for idle shutdown

    resp = client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    assert resp.status_code == 202 and resp.json()["state"] == "STOPPING"
    assert publisher.jobs[-1] == {"action": "stop", "workspace_id": ws["id"], "ordering_key": ws["id"],
                                  "worker": "worker-1"}

    report(ws["id"], "STOPPED", "worker-1")
    stopped = row(ws["id"])
    assert stopped["state"] == "STOPPED"
    assert stopped["worker_id"] is None  # its files are only in Cloud Storage now

    resp = client.post(f"/api/workspaces/{ws['id']}/start", headers=alice)
    assert resp.status_code == 202 and resp.json()["state"] == "PENDING"
    assert publisher.jobs[-1]["action"] == "start" and publisher.jobs[-1]["worker"] == "worker-1"


# Only running workspaces can be stopped.
@pytest.mark.parametrize("state", ["PENDING", "STOPPING", "STOPPED", "FAILED"])
def test_only_running_workspaces_can_be_stopped(client, signup, heartbeat, create, sql, publisher, state):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = create(alice)
    sql("UPDATE workspaces SET state = %s WHERE id = %s", (state, ws["id"]))
    jobs = len(publisher.jobs)
    resp = client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    assert resp.status_code == 409
    assert len(publisher.jobs) == jobs


# Only stopped or failed workspaces can be started.
@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "STOPPING"])
def test_only_stopped_or_failed_workspaces_can_be_started(client, signup, heartbeat, create, sql, publisher, state):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = create(alice)
    sql("UPDATE workspaces SET state = %s WHERE id = %s", (state, ws["id"]))
    jobs = len(publisher.jobs)
    resp = client.post(f"/api/workspaces/{ws['id']}/start", headers=alice)
    assert resp.status_code == 409
    assert len(publisher.jobs) == jobs


# A failed workspace can be started again.
def test_a_failed_workspace_can_be_started_again(client, signup, heartbeat, create, report, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = create(alice)
    report(ws["id"], "FAILED", "worker-1", "start failed: image not found")
    assert row(ws["id"])["error"] == "start failed: image not found"
    resp = client.post(f"/api/workspaces/{ws['id']}/start", headers=alice)
    assert resp.status_code == 202
    assert row(ws["id"])["error"] is None  # a new start clears the old error


# ---------------------------------------------------------------------------- delete


# Deleting a running workspace sends the job to its worker.
def test_delete_a_running_workspace_sends_the_job_to_its_worker(client, signup, heartbeat, running, publisher, row):
    heartbeat("worker-1")
    heartbeat("worker-2")
    alice = signup("alice")
    ws = running(alice)
    resp = client.delete(f"/api/workspaces/{ws['id']}", headers=alice)
    assert resp.status_code == 204
    assert row(ws["id"]) is None
    assert publisher.jobs[-1] == {"action": "delete", "workspace_id": ws["id"], "ordering_key": ws["id"],
                                  "worker": ws["worker"]}


# Deleting a stopped workspace uses any live worker.
def test_delete_a_stopped_workspace_uses_any_live_worker(client, signup, heartbeat, running, report, publisher, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    report(ws["id"], "STOPPED", "worker-1")
    assert client.delete(f"/api/workspaces/{ws['id']}", headers=alice).status_code == 204
    assert publisher.jobs[-1]["action"] == "delete" and publisher.jobs[-1]["worker"] == "worker-1"
    assert row(ws["id"]) is None


# Delete keeps the workspace when no worker is live.
def test_delete_without_a_live_worker_keeps_the_workspace(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    sql("UPDATE workers SET last_heartbeat = now() - interval '10 minutes'")
    sql("UPDATE workers SET state = 'DOWN'")
    resp = client.delete(f"/api/workspaces/{ws['id']}", headers=alice)
    assert resp.status_code == 503
    assert row(ws["id"]) is not None  # still on the dashboard, so the user can try again


# ------------------------------------------------------------------------------ open


# Open returns a link to the workspace's worker.
def test_open_returns_a_link_to_the_workspaces_worker(client, signup, heartbeat, running):
    heartbeat("worker-2")
    alice = signup("alice")
    ws = running(alice)
    resp = client.get(f"/api/workspaces/{ws['id']}/open", headers=alice)
    assert resp.status_code == 200
    url = resp.json()["url"]
    base, token = url.split("?token=")
    assert base == f"http://ws-{ws['id']}.worker-2.test/"
    claims = jwt.decode(token, "test-token-secret", algorithms=["HS256"])
    assert claims["typ"] == "link" and claims["ws"] == ws["id"]
    assert claims["exp"] - claims["iat"] == 300  # the link works for 5 minutes


# Only running workspaces can be opened.
@pytest.mark.parametrize("state", ["PENDING", "STOPPING", "STOPPED", "FAILED"])
def test_only_running_workspaces_can_be_opened(client, signup, heartbeat, create, sql, state):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = create(alice)
    sql("UPDATE workspaces SET state = %s WHERE id = %s", (state, ws["id"]))
    resp = client.get(f"/api/workspaces/{ws['id']}/open", headers=alice)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The workspace is not running"


# ------------------------------------------------------------------ agent reports


# A late report from a worker the workspace moved away from is ignored.
def test_report_from_another_worker_is_ignored(client, signup, heartbeat, create, report, row):
    heartbeat("worker-1")
    ws = create(signup("alice"))
    report(ws["id"], "RUNNING", "worker-2")
    assert row(ws["id"])["state"] == "PENDING"
    report(ws["id"], "RUNNING", "worker-1")
    assert row(ws["id"])["state"] == "RUNNING"


# Duplicate and out-of-order reports are ignored.
def test_duplicate_and_out_of_order_reports_are_ignored(client, signup, heartbeat, running, report, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    report(ws["id"], "RUNNING", "worker-1")  # the same start job, delivered twice
    assert row(ws["id"])["state"] == "RUNNING"

    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    report(ws["id"], "RUNNING", "worker-1")  # a stale start report arriving after the stop request
    assert row(ws["id"])["state"] == "STOPPING"

    report(ws["id"], "STOPPED", "worker-1")
    report(ws["id"], "STOPPED", "worker-1")  # the stop job, delivered twice
    report(ws["id"], "FAILED", "worker-1", "late failure")  # a failure from an older job
    stopped = row(ws["id"])
    assert stopped["state"] == "STOPPED" and stopped["error"] is None


# A report for a deleted workspace is harmless.
def test_report_for_a_deleted_workspace_is_harmless(client, signup, heartbeat, running, report, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    client.delete(f"/api/workspaces/{ws['id']}", headers=alice)
    report(ws["id"], "STOPPED", "worker-1")
    assert row(ws["id"]) is None


# Failed reports record the error.
def test_failed_reports_record_the_error(client, signup, heartbeat, running, report, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    report(ws["id"], "FAILED", "worker-1", "stop failed: disk full")
    failed = row(ws["id"])
    assert failed["state"] == "FAILED" and failed["error"] == "stop failed: disk full"
    listed = client.get("/api/workspaces", headers=alice).json()["workspaces"][0]
    assert listed["error"] == "stop failed: disk full"  # the dashboard shows it


# Unknown report states are rejected.
def test_unknown_report_states_are_rejected(client):
    import uuid
    resp = client.post(f"/internal/workspaces/{uuid.uuid4()}/state",
                       json={"state": "EXPLODED", "worker_id": "worker-1"},
                       headers={"X-Internal-Token": "test-internal-token"})
    assert resp.status_code == 400


# ------------------------------------------------------------ Pub/Sub unavailable


# Create marks the workspace failed when Pub/Sub is unavailable.
def test_create_when_pubsub_fails(client, signup, heartbeat, publisher, row):
    heartbeat("worker-1")
    alice = signup("alice")
    publisher.fail = True
    resp = client.post("/api/workspaces", json={"name": "ws"}, headers=alice)
    assert resp.status_code == 503
    ws = client.get("/api/workspaces", headers=alice).json()["workspaces"][0]
    assert ws["state"] == "FAILED" and ws["error"] == "Could not queue the request"
    assert publisher.resumed == [ws["id"]]  # the ordering key is resumed for the next job


# Stop leaves the workspace running when Pub/Sub is unavailable.
def test_stop_when_pubsub_fails_leaves_it_running(client, signup, heartbeat, running, publisher, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    publisher.fail = True
    assert client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice).status_code == 503
    assert row(ws["id"])["state"] == "RUNNING"


# Delete keeps the workspace when Pub/Sub is unavailable.
def test_delete_when_pubsub_fails_keeps_the_workspace(client, signup, heartbeat, running, publisher, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    publisher.fail = True
    assert client.delete(f"/api/workspaces/{ws['id']}", headers=alice).status_code == 503
    assert row(ws["id"]) is not None


# ----------------------------------------------- the agent's check before a job


# Agents can look up a workspace's state and worker.
def test_agents_can_check_a_workspaces_state_and_worker(client, signup, heartbeat, running, report):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    internal = {"X-Internal-Token": "test-internal-token"}
    assert client.get(f"/internal/workspaces/{ws['id']}", headers=internal).json() == \
        {"state": "RUNNING", "worker_id": "worker-1"}
    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    report(ws["id"], "STOPPED", "worker-1")
    assert client.get(f"/internal/workspaces/{ws['id']}", headers=internal).json() == \
        {"state": "STOPPED", "worker_id": None}

    assert client.get(f"/internal/workspaces/{ws['id']}").status_code == 403  # internal only
    import uuid
    assert client.get(f"/internal/workspaces/{uuid.uuid4()}", headers=internal).status_code == 404
    assert client.get("/internal/workspaces/not-a-uuid", headers=internal).status_code == 404
