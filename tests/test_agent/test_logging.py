"""The agent's structured logs, in the same format as the API's."""
import json
import sys
import uuid
from pathlib import Path

import agent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import find_log  # noqa: E402


class Message:
    # Wraps a job like a Pub/Sub message.
    def __init__(self, action, ws_id):
        self.data = json.dumps({"action": action, "workspace_id": ws_id}).encode()

    # Acknowledging does nothing in these tests.
    def ack(self):
        pass

    # Redelivery does nothing in these tests.
    def nack(self):
        pass


# A failed job is logged as an error.
def test_a_failed_job_is_logged_as_an_error(monkeypatch, capsys):
    # A start that always fails.
    def broken(ws_id):
        raise RuntimeError("disk full")
    monkeypatch.setitem(agent.ACTIONS, "start", broken)
    monkeypatch.setattr(agent, "still_mine", lambda action, ws_id: True)
    monkeypatch.setattr(agent, "report", lambda *args, **kwargs: None)
    ws_id = str(uuid.uuid4())
    agent.handle(Message("start", ws_id))
    out = capsys.readouterr().out
    [received] = find_log(out, "job received")
    assert received == {"severity": "INFO", "message": "job received", "component": "agent",
                        "worker": "test-worker", "action": "start", "workspace_id": ws_id}
    [failed] = find_log(out, "job failed")
    assert failed["severity"] == "ERROR" and failed["workspace_id"] == ws_id and "disk full" in failed["error"]


# A skipped stale job is logged as a warning.
def test_a_skipped_stale_job_is_logged_as_a_warning(monkeypatch, capsys):
    monkeypatch.setattr(agent, "still_mine", lambda action, ws_id: False)
    ws_id = str(uuid.uuid4())
    agent.handle(Message("start", ws_id))
    [skipped] = find_log(capsys.readouterr().out, "skipping a job that is no longer this worker's")
    assert skipped["severity"] == "WARNING" and skipped["workspace_id"] == ws_id
