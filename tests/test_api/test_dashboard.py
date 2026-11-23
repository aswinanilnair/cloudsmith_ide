"""What the dashboard shows beyond the workspace list: the worker pool (GET /api/workers) and each
workspace's details (its worker's VM name and uptime)."""
import pytest
from app import main


# The worker list needs a session.
def test_the_worker_list_needs_a_session(client):
    assert client.get("/api/workers").status_code == 401


# The worker list shows each worker's VM, status, address and load.
def test_the_worker_list_shows_each_workers_status_address_and_load(client, signup, heartbeat, create, sql):
    heartbeat("worker-1", capacity=3, vm="cloudsmith-worker-1")
    heartbeat("worker-2", capacity=2)  # an agent that doesn't send its VM name
    alice = signup("alice")
    placed = [create(alice, name)["worker"] for name in ("a", "b", "c")]

    pool = client.get("/api/workers", headers=alice).json()
    assert pool["online"] == 2 and pool["total"] == 2 and pool["capacity"] == 5 and pool["used"] == 3
    by_id = {w["id"]: w for w in pool["workers"]}
    assert by_id["worker-1"]["vm"] == "cloudsmith-worker-1"
    assert by_id["worker-2"]["vm"] == "worker-2"  # falls back to the worker ID
    assert by_id["worker-1"]["address"] == "worker-1.test"
    assert {w: by_id[w]["used"] for w in by_id} == {w: placed.count(w) for w in by_id}
    assert all(w["status"] == "online" and w["last_heartbeat_s"] < 5 for w in pool["workers"])


# A quiet worker is shown as late, then offline.
@pytest.mark.parametrize("silent, status", [("5 seconds", "online"), ("30 seconds", "late"),
                                            ("60 seconds", "offline")])
def test_a_quiet_worker_is_shown_late_then_offline(client, signup, heartbeat, sql, silent, status):
    heartbeat("worker-1")
    sql(f"UPDATE workers SET last_heartbeat = now() - interval '{silent}'")
    worker = client.get("/api/workers", headers=signup("alice")).json()["workers"][0]
    assert worker["status"] == status


# Offline workers do not count toward free slots.
def test_offline_workers_do_not_count_toward_free_slots(client, signup, heartbeat, sql):
    heartbeat("worker-1", capacity=3)
    heartbeat("worker-2", capacity=4)
    sql("UPDATE workers SET last_heartbeat = now() - interval '60 seconds' WHERE id = 'worker-2'")
    heartbeat("worker-1")  # the sweep marks worker-2 down
    pool = client.get("/api/workers", headers=signup("alice")).json()
    assert (pool["online"], pool["total"], pool["capacity"]) == (1, 2, 3)


# worker_address extracts a worker's address from its workspace URL.
@pytest.mark.parametrize("url, address", [
    ("http://ws-{id}.34.57.230.194.sslip.io/", "34.57.230.194"),
    ("http://ws-{id}.localhost:8080/", "localhost:8080"),
    ("http://{id}.example.test/", "x.example.test"),  # not the usual ws-{id} form: shown whole
])
def test_worker_address(url, address):
    assert main.worker_address(url) == address


# A running workspace shows its worker VM and uptime; both clear when it stops.
def test_a_workspace_shows_its_worker_vm_and_uptime_while_running(client, signup, heartbeat, running, report, sql):
    heartbeat("worker-1", vm="cloudsmith-worker-1")
    alice = signup("alice")
    ws = running(alice)
    sql("UPDATE workspaces SET started_at = now() - interval '90 seconds'")

    listed = client.get("/api/workspaces", headers=alice).json()["workspaces"][0]
    assert listed["worker_vm"] == "cloudsmith-worker-1"
    assert listed["started_at"] is not None

    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    report(ws["id"], "STOPPED", "worker-1")
    listed = client.get("/api/workspaces", headers=alice).json()["workspaces"][0]
    assert listed["worker"] is None and listed["worker_vm"] is None and listed["started_at"] is None


# Uptime restarts when a workspace starts again.
def test_uptime_restarts_when_a_workspace_starts_again(client, signup, heartbeat, running, report, row, sql):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    sql("UPDATE workspaces SET started_at = now() - interval '1 hour'")
    sql("UPDATE workspaces SET state = 'PENDING'")  # e.g. restarted on another worker after recovery
    report(ws["id"], "RUNNING", "worker-1")
    started = row(ws["id"])["started_at"]
    assert (main.db.fetchone("SELECT now() - %s::timestamptz AS age", (started,))["age"]).total_seconds() < 5


# Workspaces already running when started_at was added get an uptime.
def test_workspaces_running_before_started_at_existed_get_an_uptime(client, signup, heartbeat, running, row, sql):
    """started_at was added while workspaces were running; the schema fills it in from when their
    state last changed, i.e. when they became RUNNING."""
    heartbeat("worker-1")
    ws = running(signup("alice"))
    sql("UPDATE workspaces SET started_at = NULL, updated_at = now() - interval '2 hours'")
    main.db.init()  # what every API start does
    assert row(ws["id"])["started_at"] == row(ws["id"])["updated_at"]
