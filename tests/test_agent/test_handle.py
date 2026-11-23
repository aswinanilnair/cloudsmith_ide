"""How the agent handles a Pub/Sub message: what it reports to the API, and whether it acks
the message (done) or nacks it (Pub/Sub delivers it again). A failed job must never be
silently dropped. The jobs themselves are replaced with stand-ins here."""
import json
import uuid

import agent
import pytest
import requests


class FakeMessage:
    # Wraps job data like a Pub/Sub message.
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.acked = self.nacked = False

    # Records that the message was acknowledged.
    def ack(self):
        self.acked = True

    # Records that the message was sent back for redelivery.
    def nack(self):
        self.nacked = True


# Stands in for the API's check that a job still belongs to this worker.
@pytest.fixture(autouse=True)
def owner(monkeypatch):
    """What the API says before each start or stop: by default, the job is still this
    worker's. A test sets owner["mine"] = False for a stale job, or owner["error"] to make
    the check fail."""
    answer = {"mine": True, "error": None, "asked": []}

    # Answers the ownership check as the test has set it up.
    def still_mine(action, ws_id):
        answer["asked"].append(action)
        if answer["error"]:
            raise answer["error"]
        return answer["mine"]

    monkeypatch.setattr(agent, "still_mine", still_mine)
    return answer


# Records the agent's state reports instead of sending them.
@pytest.fixture
def reports(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "report", lambda ws_id, state, error=None: sent.append((ws_id, state, error)))
    return sent


# Replaces start, stop and delete with stand-ins that record calls or fail on request.
@pytest.fixture
def jobs(monkeypatch):
    calls, failing = [], set()

    # Builds a stand-in for one action.
    def stand_in(action):
        # Records the call, and fails if this action is set to fail.
        def run(ws_id):
            calls.append((action, ws_id))
            if action in failing:
                raise RuntimeError("disk full")
        return run

    for action in ("start", "stop", "delete"):
        monkeypatch.setitem(agent.ACTIONS, action, stand_in(action))
    return calls, failing


# Builds a Pub/Sub message carrying a job.
def job(action, ws_id=None):
    return FakeMessage({"action": action, "workspace_id": ws_id or str(uuid.uuid4())})


# A successful job is reported to the API and acknowledged.
@pytest.mark.parametrize("action, state", [("start", "RUNNING"), ("stop", "STOPPED")])
def test_a_successful_job_is_reported_and_acked(jobs, reports, action, state):
    ws_id = str(uuid.uuid4())
    msg = job(action, ws_id)
    agent.handle(msg)
    assert jobs[0] == [(action, ws_id)]
    assert reports == [(ws_id, state, None)]
    assert msg.acked and not msg.nacked


# A successful delete reports nothing, since the workspace is gone.
def test_a_delete_reports_nothing(jobs, reports):
    msg = job("delete")
    agent.handle(msg)
    assert reports == [] and msg.acked


# A failed job is reported with its error.
@pytest.mark.parametrize("action", ["start", "stop"])
def test_a_failed_job_is_reported_with_its_error(jobs, reports, action):
    jobs[1].add(action)
    ws_id = str(uuid.uuid4())
    msg = job(action, ws_id)
    agent.handle(msg)
    assert reports == [(ws_id, "FAILED", f"{action} failed: disk full")]  # the dashboard shows it
    assert msg.acked  # the user can retry from the dashboard


# A failed delete is redelivered, since only a retry can finish it.
def test_a_failed_delete_is_retried(jobs, reports):
    """The workspace is already gone from the dashboard, so only redelivery can finish it."""
    jobs[1].add("delete")
    msg = job("delete")
    agent.handle(msg)
    assert msg.nacked and not msg.acked
    assert reports == []


# Malformed jobs are acknowledged and dropped without running anything.
@pytest.mark.parametrize("data", [
    b"not json",
    {"action": "explode", "workspace_id": str(uuid.uuid4())},
    {"action": "start", "workspace_id": "not-a-uuid"},
    {"action": "start"},
])
def test_malformed_jobs_are_dropped(jobs, reports, data):
    msg = FakeMessage(data)
    agent.handle(msg)
    assert msg.acked  # retrying would never help
    assert jobs[0] == [] and reports == []


# ----------------------------------------------------- when the API can't be reached


# Makes the agent's report request get a given HTTP status or error.
def api_reply(monkeypatch, status=None, error=None):
    """Make the agent's report request get this HTTP status, or raise this error."""
    sent = []

    # Records the report request and answers as set up.
    def post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "headers": headers})
        if error:
            raise error
        resp = requests.Response()
        resp.status_code, resp.url = status, url
        return resp

    monkeypatch.setattr(agent.requests, "post", post)
    return sent


# A report carries the workspace state and this worker's ID.
def test_report_sends_the_state_with_this_workers_id(jobs, monkeypatch):
    sent = api_reply(monkeypatch, status=204)
    ws_id = str(uuid.uuid4())
    agent.handle(job("start", ws_id))
    assert sent == [{
        "url": f"http://127.0.0.1:1/internal/workspaces/{ws_id}/state",
        "json": {"state": "RUNNING", "error": None, "worker_id": "test-worker"},
        "headers": {"X-Internal-Token": "test-internal-token"},
    }]


# A job whose result cannot be reported is redelivered.
@pytest.mark.parametrize("status, error", [
    (None, requests.ConnectionError("API unreachable")),
    (500, None),
    (503, None),
])
def test_a_job_whose_result_cannot_be_reported_is_retried(jobs, monkeypatch, status, error):
    api_reply(monkeypatch, status=status, error=error)
    msg = job("start")
    agent.handle(msg)
    assert msg.nacked and not msg.acked


# A report the API rejects is not retried forever.
def test_a_report_the_api_rejects_is_not_retried_forever(jobs, monkeypatch):
    api_reply(monkeypatch, status=403)  # e.g. a wrong INTERNAL_TOKEN: every retry would fail too
    msg = job("start")
    agent.handle(msg)
    assert msg.acked and not msg.nacked


# ------------------------------------------------- jobs that are no longer this worker's


# A job that no longer belongs to this worker is skipped.
@pytest.mark.parametrize("action", ["start", "stop"])
def test_a_stale_job_is_skipped(jobs, reports, owner, action):
    """E.g. a start that waited in this worker's subscription while the worker was down,
    for a workspace the API has since restarted on another worker."""
    owner["mine"] = False
    msg = job(action)
    agent.handle(msg)
    assert jobs[0] == []          # nothing was touched
    assert reports == []
    assert msg.acked and not msg.nacked


# Delete jobs run without asking the API first.
def test_delete_runs_without_asking(jobs, reports, owner):
    owner["mine"] = False
    agent.handle(job("delete"))
    assert [action for action, _ in jobs[0]] == ["delete"]
    assert owner["asked"] == []


# A job is redelivered when the ownership check cannot reach the API.
def test_a_job_that_cannot_be_checked_is_retried(jobs, reports, owner):
    owner["error"] = requests.ConnectionError("API unreachable")
    msg = job("start")
    agent.handle(msg)
    assert jobs[0] == [] and msg.nacked and not msg.acked


# A job is dropped when the API rejects its ownership check.
def test_a_job_whose_check_the_api_rejects_is_dropped(jobs, reports, owner):
    resp = requests.Response()
    resp.status_code = 403
    owner["error"] = requests.HTTPError(response=resp)
    msg = job("start")
    agent.handle(msg)
    assert jobs[0] == [] and msg.acked


# ------------------------------------------------------------- the check itself


# still_mine answers from the API's view of the workspace's state and worker.
@pytest.mark.parametrize("action, status, body, expected", [
    ("start", 200, {"state": "PENDING", "worker_id": "test-worker"}, True),
    ("start", 200, {"state": "PENDING", "worker_id": "worker-2"}, False),    # moved elsewhere
    ("start", 200, {"state": "RUNNING", "worker_id": "test-worker"}, False),  # already done
    ("stop", 200, {"state": "STOPPING", "worker_id": "test-worker"}, True),
    ("stop", 200, {"state": "STOPPED", "worker_id": None}, False),           # stopped meanwhile
    ("stop", 200, {"state": "PENDING", "worker_id": "test-worker"}, False),  # started again since
    ("start", 404, {"detail": "Workspace not found"}, False),                 # deleted
])
def test_still_mine(monkeypatch, action, status, body, expected):
    seen = []

    # Records the request and answers with the given status and body.
    def get(url, headers=None, timeout=None):
        seen.append((url, headers))
        resp = requests.Response()
        resp.status_code, resp.url, resp._content = status, url, json.dumps(body).encode()
        return resp

    monkeypatch.undo()  # use the real still_mine, not the autouse stand-in
    monkeypatch.setattr(agent.requests, "get", get)
    ws_id = str(uuid.uuid4())
    assert agent.still_mine(action, ws_id) is expected
    assert seen == [(f"http://127.0.0.1:1/internal/workspaces/{ws_id}", {"X-Internal-Token": "test-internal-token"})]


# still_mine raises when the API returns an error.
def test_still_mine_raises_when_the_api_fails(monkeypatch):
    # Answers every request with a server error.
    def get(url, headers=None, timeout=None):
        resp = requests.Response()
        resp.status_code, resp.url = 500, url
        return resp

    monkeypatch.undo()
    monkeypatch.setattr(agent.requests, "get", get)
    with pytest.raises(requests.HTTPError):
        agent.still_mine("start", str(uuid.uuid4()))
