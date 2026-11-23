"""Workers: placement, heartbeats, recovery from a dead worker, and idle shutdown.

Time is moved forward by backdating timestamps in the database (a worker's last heartbeat,
a workspace's last use), so no test has to wait."""
import threading
import time

import pytest
from app import main
from fastapi import HTTPException

DEAD = "now() - interval '60 seconds'"  # older than WORKER_TIMEOUT_SECONDS (45)


# -------------------------------------------------------------------------- placement


# Workspaces are spread over the workers.
def test_workspaces_spread_over_workers(client, signup, heartbeat, create):
    heartbeat("worker-1", capacity=3)
    heartbeat("worker-2", capacity=3)
    workers = [create(signup(f"user{i}"))["worker"] for i in range(4)]
    assert sorted(workers) == ["worker-1", "worker-1", "worker-2", "worker-2"]


# Full workers are skipped.
def test_full_workers_are_skipped(client, signup, heartbeat, create):
    heartbeat("worker-1", capacity=1)
    heartbeat("worker-2", capacity=1)
    alice = signup("alice")
    assert {create(alice, "a")["worker"], create(alice, "b")["worker"]} == {"worker-1", "worker-2"}
    resp = client.post("/api/workspaces", json={"name": "c"}, headers=alice)
    assert resp.status_code == 503
    assert resp.json()["detail"] == main.NO_CAPACITY


# Stopped workspaces free their slot.
def test_stopped_workspaces_free_their_slot(client, signup, heartbeat, running, report, create):
    heartbeat("worker-1", capacity=1)
    alice = signup("alice")
    ws = running(alice)
    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    report(ws["id"], "STOPPED", "worker-1")
    assert create(alice, "second")["worker"] == "worker-1"


# Workers without recent heartbeats get no workspaces.
def test_workers_without_recent_heartbeats_get_no_workspaces(client, signup, heartbeat, create, sql):
    heartbeat("worker-1", capacity=10)
    heartbeat("worker-2", capacity=1)
    sql(f"UPDATE workers SET last_heartbeat = {DEAD} WHERE id = 'worker-1'")
    assert create(signup("alice"))["worker"] == "worker-2"


# A failed workspace restarts on its previous worker, where its container may still be.
def test_a_failed_workspace_restarts_on_its_previous_worker(client, signup, heartbeat, create, report):
    heartbeat("worker-1", capacity=3)
    alice = signup("alice")
    ws = create(alice)
    report(ws["id"], "FAILED", "worker-1", "stop failed: upload error")
    heartbeat("worker-2", capacity=10)  # more free slots, but not where the files are
    resp = client.post(f"/api/workspaces/{ws['id']}/start", headers=alice)
    assert resp.json()["worker"] == "worker-1"


# Concurrent creates never overfill a worker.
def test_concurrent_creates_never_overfill_a_worker(client, signup, heartbeat, sql):
    """Placement takes a lock, so two requests cannot both take a worker's last slot.

    Eight users race for two slots, several rounds over. The calls go straight to the endpoint
    function from threads, with the connection pool filled first so they really overlap."""
    from app import db
    db.pool.resize(min_size=5, max_size=5)
    db.pool.wait(timeout=30)
    try:
        users = []
        for i in range(8):
            signup(f"racer{i}")
            users.append({"id": str(db.fetchone("SELECT id FROM users WHERE username = %s", (f"racer{i}",))["id"]),
                          "username": f"racer{i}"})
        heartbeat("worker-1", capacity=2)
        for _ in range(5):
            sql("DELETE FROM workspaces")
            results, barrier = [], threading.Barrier(len(users))

            # Waits for all threads, then tries to create a workspace.
            def attempt(user):
                barrier.wait()
                try:
                    main.create_workspace(main.CreateWorkspace(name="race"), user=user)
                    results.append("created")
                except HTTPException as exc:
                    results.append(exc.status_code)

            threads = [threading.Thread(target=attempt, args=(u,)) for u in users]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            assert db.fetchone("SELECT count(*) AS n FROM workspaces WHERE worker_id = 'worker-1'")["n"] == 2
            assert sorted(results, key=str) == [503] * 6 + ["created"] * 2
    finally:
        db.pool.resize(min_size=1, max_size=5)


# ------------------------------------------------------------------------- heartbeats


# A heartbeat registers the worker and returns its workspaces.
def test_heartbeat_registers_the_worker_and_lists_its_workspaces(client, signup, heartbeat, create):
    assert heartbeat("worker-1") == {"assigned": []}
    ws = create(signup("alice"))
    assert heartbeat("worker-1") == {"assigned": [ws["id"]]}
    assert heartbeat("worker-2") == {"assigned": []}


# Malformed heartbeats are rejected.
@pytest.mark.parametrize("worker_id, body", [
    ("Worker_1", {"workspace_url": "http://ws-{id}.test/", "capacity": 3}),
    ("worker-1", {"workspace_url": "http://no-placeholder.test/", "capacity": 3}),
    ("worker-1", {"workspace_url": "http://ws-{id}.test/", "capacity": -1}),
])
def test_bad_heartbeats_are_rejected(client, worker_id, body):
    resp = client.post(f"/internal/workers/{worker_id}/heartbeat", json=body,
                       headers={"X-Internal-Token": "test-internal-token"})
    assert resp.status_code == 400


# ------------------------------------------------------------------------ recovery


# A dead worker's running workspaces restart on another worker.
def test_a_dead_workers_workspaces_restart_on_another_worker(client, signup, heartbeat, running, report,
                                                              sql, row, publisher):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    assert ws["worker"] == "worker-1"
    heartbeat("worker-2")

    sql(f"UPDATE workers SET last_heartbeat = {DEAD} WHERE id = 'worker-1'")
    heartbeat("worker-2")  # any heartbeat runs the check

    moved = row(ws["id"])
    assert moved["state"] == "PENDING" and moved["worker_id"] == "worker-2"
    assert moved["error"] == "Its worker stopped responding; restarting it on another worker"
    assert publisher.jobs[-1] == {"action": "start", "workspace_id": ws["id"], "ordering_key": ws["id"],
                                  "worker": "worker-2"}

    report(ws["id"], "RUNNING", "worker-1")  # a late report from the old worker
    assert row(ws["id"])["state"] == "PENDING" and row(ws["id"])["worker_id"] == "worker-2"
    report(ws["id"], "RUNNING", "worker-2")
    assert row(ws["id"])["state"] == "RUNNING" and row(ws["id"])["error"] is None

    # The old worker comes back: it is told the workspace is no longer its own.
    assert heartbeat("worker-1") == {"assigned": []}
    assert heartbeat("worker-2") == {"assigned": [ws["id"]]}


# A pending workspace on a dead worker moves too.
def test_a_pending_workspace_on_a_dead_worker_moves_too(client, signup, heartbeat, create, sql, row):
    heartbeat("worker-1")
    ws = create(signup("alice"))
    heartbeat("worker-2")
    sql(f"UPDATE workers SET last_heartbeat = {DEAD} WHERE id = 'worker-1'")
    heartbeat("worker-2")
    assert row(ws["id"])["worker_id"] == "worker-2"


# A stopping workspace on a dead worker is marked stopped.
def test_a_stopping_workspace_on_a_dead_worker_is_marked_stopped(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    client.post(f"/api/workspaces/{ws['id']}/stop", headers=alice)
    heartbeat("worker-2")
    sql(f"UPDATE workers SET last_heartbeat = {DEAD} WHERE id = 'worker-1'")
    heartbeat("worker-2")
    stopped = row(ws["id"])
    assert stopped["state"] == "STOPPED" and stopped["worker_id"] is None
    assert stopped["error"] == "Its worker stopped responding; start it again (recent changes may be lost)"


# Without room elsewhere, a dead worker's workspace is stopped.
def test_without_room_elsewhere_a_dead_workers_workspace_is_stopped(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-2", capacity=1)
    other = running(signup("bob"))           # fills worker-2
    heartbeat("worker-1", capacity=3)
    ws = running(signup("alice"))            # on worker-1
    assert (other["worker"], ws["worker"]) == ("worker-2", "worker-1")
    sql(f"UPDATE workers SET last_heartbeat = {DEAD} WHERE id = 'worker-1'")
    heartbeat("worker-2", capacity=1)
    stopped = row(ws["id"])
    assert stopped["state"] == "STOPPED" and stopped["worker_id"] is None
    assert "start it again" in stopped["error"]


# A restart job that cannot be queued marks the workspace failed.
def test_restart_job_that_cannot_be_queued_marks_the_workspace_failed(client, signup, heartbeat, running,
                                                                      sql, row, publisher):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    heartbeat("worker-2")
    sql(f"UPDATE workers SET last_heartbeat = {DEAD} WHERE id = 'worker-1'")
    publisher.fail = True
    heartbeat("worker-2")
    failed = row(ws["id"])
    assert failed["state"] == "FAILED" and failed["error"] == "Could not queue the restart"


# --------------------------------------------------------------------- idle shutdown


# Marks a workspace as last used some minutes ago.
def idle_for(sql, ws_id, minutes):
    sql(f"UPDATE workspaces SET last_active = now() - interval '{minutes} minutes' WHERE id = %s", (ws_id,))


# Idle workspaces are stopped with a note on the dashboard.
def test_idle_workspaces_are_stopped_with_a_note(client, signup, heartbeat, running, report, sql, row, publisher):
    heartbeat("worker-1")
    alice = signup("alice")
    ws = running(alice)
    idle_for(sql, ws["id"], 31)
    heartbeat("worker-1")

    stopping = row(ws["id"])
    note = "Stopped automatically after 30 minutes without use"
    assert stopping["state"] == "STOPPING" and stopping["error"] == note
    assert publisher.jobs[-1] == {"action": "stop", "workspace_id": ws["id"], "ordering_key": ws["id"],
                                  "worker": "worker-1"}

    report(ws["id"], "STOPPED", "worker-1")
    assert row(ws["id"])["error"] == note  # the dashboard keeps showing why it stopped
    client.post(f"/api/workspaces/{ws['id']}/start", headers=alice)
    assert row(ws["id"])["error"] is None  # until it is started again


# Recently used workspaces keep running.
def test_recently_used_workspaces_keep_running(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    idle_for(sql, ws["id"], 29)
    heartbeat("worker-1")
    assert row(ws["id"])["state"] == "RUNNING"


# Activity reported by the proxy keeps a workspace running.
def test_activity_reported_by_the_proxy_keeps_a_workspace_running(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    idle_for(sql, ws["id"], 31)
    heartbeat("worker-1", activity={ws["id"]: time.time()})  # e.g. the editor tab is open
    assert row(ws["id"])["state"] == "RUNNING"


# Activity reported by another worker does not count.
def test_activity_from_another_worker_does_not_count(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    heartbeat("worker-2")
    idle_for(sql, ws["id"], 31)
    heartbeat("worker-2", activity={ws["id"]: time.time()})
    assert row(ws["id"])["state"] == "STOPPING"


# Older activity never moves the last use backwards.
def test_older_activity_never_moves_last_use_backwards(client, signup, heartbeat, running, sql, row):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    before = row(ws["id"])["last_active"]
    heartbeat("worker-1", activity={ws["id"]: time.time() - 3600, "not-a-uuid": 1.0})  # bad ids are skipped
    assert row(ws["id"])["last_active"] == before


# Idle shutdown can be turned off.
def test_idle_shutdown_can_be_turned_off(client, signup, heartbeat, running, sql, row, monkeypatch):
    monkeypatch.setattr(main, "IDLE_TIMEOUT_MIN", 0)
    heartbeat("worker-1")
    ws = running(signup("alice"))
    idle_for(sql, ws["id"], 600)
    heartbeat("worker-1")
    assert row(ws["id"])["state"] == "RUNNING"


# The idle note uses the singular for one minute.
def test_idle_note_uses_the_singular_for_one_minute(client, signup, heartbeat, running, sql, row, monkeypatch):
    monkeypatch.setattr(main, "IDLE_TIMEOUT_MIN", 1)
    heartbeat("worker-1")
    ws = running(signup("alice"))
    idle_for(sql, ws["id"], 2)
    heartbeat("worker-1")
    assert row(ws["id"])["error"] == "Stopped automatically after 1 minute without use"


# An idle stop that cannot be queued is retried later.
def test_idle_stop_that_cannot_be_queued_is_retried_later(client, signup, heartbeat, running, sql, row, publisher):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    idle_for(sql, ws["id"], 31)
    publisher.fail = True
    heartbeat("worker-1")
    assert row(ws["id"])["state"] == "RUNNING"
    publisher.fail = False
    heartbeat("worker-1")
    assert row(ws["id"])["state"] == "STOPPING"
