"""Accounts and sign-in, and that one user can never act on another user's workspace."""
import uuid

import pytest
from app import main


# Sign-up returns a session that works for the dashboard API.
def test_signup_returns_a_working_session(client):
    resp = client.post("/api/signup", json={"username": "  Alice ", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "alice"  # trimmed and lower-cased
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["username"] == "alice"


# Sign-up rejects invalid usernames.
@pytest.mark.parametrize("username", ["ab", "x" * 33, "has space", "dots.here", "émile"])
def test_signup_rejects_bad_usernames(client, username):
    resp = client.post("/api/signup", json={"username": username, "password": "password123"})
    assert resp.status_code == 400
    assert "Username must be" in resp.json()["detail"]


# Sign-up rejects passwords that are too short.
def test_signup_rejects_short_passwords(client):
    resp = client.post("/api/signup", json={"username": "alice", "password": "short"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Password must be at least 8 characters"


# Usernames are unique regardless of case.
def test_usernames_are_unique_ignoring_case(client, signup):
    signup("alice")
    resp = client.post("/api/signup", json={"username": "ALICE", "password": "password123"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "That username is taken"


# Sign-up can be turned off.
def test_signup_can_be_turned_off(client, monkeypatch):
    monkeypatch.setattr(main, "ALLOW_SIGNUP", False)
    resp = client.post("/api/signup", json={"username": "alice", "password": "password123"})
    assert resp.status_code == 403


# Login works with the right password, and gives the same error for a wrong password or unknown user.
def test_login(client, signup):
    signup("alice", "password123")
    ok = client.post("/api/login", json={"username": "Alice", "password": "password123"})
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "alice"
    for username, password in [("alice", "wrong-password"), ("nobody", "password123")]:
        resp = client.post("/api/login", json={"username": username, "password": password})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Wrong username or password"  # same message either way


# Passwords are stored salted and hashed, never in plain text.
def test_passwords_are_stored_hashed(client, signup):
    signup("alice", "password123")
    from app import db
    stored = db.fetchone("SELECT password_hash FROM users WHERE username = 'alice'")["password_hash"]
    assert "password123" not in stored
    assert main.check_password("password123", stored)
    assert not main.check_password("password124", stored)


# Requests without a valid session are refused.
@pytest.mark.parametrize("header", [
    None,
    "Bearer not-a-token",
    "Basic YWxpY2U6cGFzc3dvcmQ=",
])
def test_requests_without_a_valid_session_are_refused(client, header):
    headers = {"Authorization": header} if header else {}
    assert client.get("/api/workspaces", headers=headers).status_code == 401


# Expired sessions are refused.
def test_expired_sessions_are_refused(client, signup):
    signup("alice")
    from app import db
    user_id = str(db.fetchone("SELECT id FROM users")["id"])
    token = main.make_token({"typ": "user", "sub": user_id, "name": "alice"}, ttl_s=-10)
    resp = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Session expired; sign in again"


# The token in an Open link does not work as a dashboard session.
def test_a_workspace_link_token_is_not_a_session(client, signup):
    signup("alice")
    from app import db
    user_id = str(db.fetchone("SELECT id FROM users")["id"])
    link = main.make_token({"typ": "link", "sub": user_id, "ws": str(uuid.uuid4())}, 300)
    assert client.get("/api/me", headers={"Authorization": f"Bearer {link}"}).status_code == 401


# Internal endpoints need the internal token.
def test_internal_endpoints_need_the_internal_token(client):
    ws_id = str(uuid.uuid4())
    body = {"state": "RUNNING", "worker_id": "worker-1"}
    hb = {"workspace_url": "http://ws-{id}.test/", "capacity": 3}
    for headers in [{}, {"X-Internal-Token": "wrong"}]:
        assert client.post(f"/internal/workspaces/{ws_id}/state", json=body, headers=headers).status_code == 403
        assert client.post("/internal/workers/worker-1/heartbeat", json=hb, headers=headers).status_code == 403


# ----------------------------------------------------------------------- ownership


# Users only see their own workspaces.
def test_users_only_see_their_own_workspaces(client, signup, heartbeat, create):
    heartbeat("worker-1")
    alice, bob = signup("alice"), signup("bob")
    create(alice, "alice-ws")
    create(bob, "bob-ws")
    names = [w["name"] for w in client.get("/api/workspaces", headers=bob).json()["workspaces"]]
    assert names == ["bob-ws"]


# Users cannot act on another user's workspace.
def test_users_cannot_act_on_another_users_workspace(client, signup, heartbeat, running, row, publisher):
    heartbeat("worker-1")
    alice, bob = signup("alice"), signup("bob")
    ws = running(alice)
    jobs_before = len(publisher.jobs)

    attempts = [
        client.post(f"/api/workspaces/{ws['id']}/stop", headers=bob),
        client.post(f"/api/workspaces/{ws['id']}/start", headers=bob),
        client.get(f"/api/workspaces/{ws['id']}/open", headers=bob),
        client.delete(f"/api/workspaces/{ws['id']}", headers=bob),
    ]
    for resp in attempts:
        assert resp.status_code == 404  # the same answer as for a workspace that does not exist
        assert resp.json()["detail"] == "Workspace not found"
    assert row(ws["id"])["state"] == "RUNNING"
    assert len(publisher.jobs) == jobs_before  # no job was sent


# Unknown workspace IDs return not found.
@pytest.mark.parametrize("ws_id", ["not-a-uuid", str(uuid.uuid4())])
def test_unknown_workspaces_are_not_found(client, signup, ws_id):
    alice = signup("alice")
    assert client.post(f"/api/workspaces/{ws_id}/stop", headers=alice).status_code == 404
    assert client.get(f"/api/workspaces/{ws_id}/open", headers=alice).status_code == 404
