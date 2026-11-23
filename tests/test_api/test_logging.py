"""The API's structured logs: what Cloud Logging, the log-based metrics and the dashboard
rely on (monitoring/)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import find_log, log_lines  # noqa: E402


# Every log line has a severity and a component.
def test_every_line_has_severity_and_component(client, signup, heartbeat, create, capsys):
    heartbeat("worker-1")
    create(signup("alice"))
    lines = log_lines(capsys.readouterr().out)
    assert lines
    for entry in lines:
        assert entry["severity"] in ("DEBUG", "INFO", "WARNING", "ERROR")
        assert entry["component"] == "api"


# Each job sent is logged with its workspace and worker.
def test_each_job_sent_is_logged_with_its_workspace_and_worker(client, signup, heartbeat, create, capsys):
    heartbeat("worker-1")
    ws = create(signup("alice"))
    [sent] = find_log(capsys.readouterr().out, "job sent")
    assert sent["action"] == "start" and sent["workspace_id"] == ws["id"] and sent["worker"] == "worker-1"
    assert sent["message_id"] == "message-id"


# Heartbeats log the worker's workspace count, which a log-based metric reads.
def test_heartbeats_log_the_workers_workspace_count(client, signup, heartbeat, create, capsys):
    heartbeat("worker-1", capacity=4)
    alice = signup("alice")
    create(alice, "a")
    create(alice, "b")
    capsys.readouterr()
    heartbeat("worker-1", capacity=4)
    [beat] = find_log(capsys.readouterr().out, "worker heartbeat")
    assert beat["worker"] == "worker-1" and beat["workspaces"] == 2 and beat["capacity"] == 4


# Failed jobs are logged as errors, which a log-based metric counts.
def test_failed_jobs_are_logged_as_errors(client, signup, heartbeat, create, report, capsys):
    heartbeat("worker-1")
    ws = create(signup("alice"))
    report(ws["id"], "FAILED", "worker-1", "start failed: disk full")
    [failed] = find_log(capsys.readouterr().out, "job failed")
    assert failed["severity"] == "ERROR"
    assert failed["workspace_id"] == ws["id"] and failed["worker"] == "worker-1"
    assert failed["error"] == "start failed: disk full"


# Ignored reports are logged as not applied.
def test_ignored_reports_are_logged_as_not_applied(client, signup, heartbeat, create, report, capsys):
    heartbeat("worker-1")
    ws = create(signup("alice"))
    report(ws["id"], "RUNNING", "worker-2")  # from a worker that doesn't own it
    report(ws["id"], "FAILED", "worker-2", "late")  # an ignored failure is not counted as one
    out = capsys.readouterr().out
    assert [e["applied"] for e in find_log(out, "state reported")] == [False, False]
    assert find_log(out, "job failed") == []


# Publish failures are logged as errors.
def test_publish_failures_are_logged_as_errors(client, signup, heartbeat, publisher, capsys):
    heartbeat("worker-1")
    alice = signup("alice")
    publisher.fail = True
    client.post("/api/workspaces", json={"name": "ws"}, headers=alice)
    [failed] = find_log(capsys.readouterr().out, "publish failed")
    assert failed["severity"] == "ERROR" and failed["action"] == "start"


# A dead worker is logged as a warning.
def test_a_dead_worker_is_logged_as_a_warning(client, signup, heartbeat, running, sql, capsys):
    heartbeat("worker-1")
    ws = running(signup("alice"))
    heartbeat("worker-2")
    sql("UPDATE workers SET last_heartbeat = now() - interval '60 seconds' WHERE id = 'worker-1'")
    heartbeat("worker-2")
    out = capsys.readouterr().out
    [down] = find_log(out, "worker stopped sending heartbeats")
    assert down["severity"] == "WARNING" and down["worker"] == "worker-1"
    [moved] = find_log(out, "recovered workspace from a dead worker")
    assert moved["workspace_id"] == ws["id"] and moved["moved_to"] == "worker-2"
