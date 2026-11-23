"""CloudSmith API: user accounts, workspaces, workers, and the dashboard page.

The API never talks to Docker. For every workspace action it updates PostgreSQL and
publishes a job to Pub/Sub for one worker. The agent on that worker runs the job and
reports the result back through POST /internal/workspaces/{id}/state.

Workers: each agent sends POST /internal/workers/{id}/heartbeat every few seconds. A
workspace is placed on a live worker when it starts, and its jobs carry a "worker"
attribute that the worker's own Pub/Sub subscription filters on. When a worker stops
sending heartbeats, its workspaces are restarted elsewhere from their Cloud Storage
archives. Workspaces nobody has used for IDLE_TIMEOUT_MINUTES are stopped. Both checks
run inside heartbeat requests, because Cloud Run only gives the API CPU while it is
handling a request.

Jobs use the workspace id as the Pub/Sub ordering key, so the jobs of one workspace
(start, stop, delete) reach the agent in the order they were sent.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1
from pydantic import BaseModel

from . import db

GCP_PROJECT = os.getenv("GCP_PROJECT", "local-project")
PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "workspace-jobs")
TOKEN_SECRET = os.getenv("TOKEN_SECRET", "dev-token-secret")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "dev-internal-token")
ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "true").lower() == "true"
MAX_WORKSPACES_PER_USER = int(os.getenv("MAX_WORKSPACES_PER_USER", "3"))
WORKER_TIMEOUT_S = int(os.getenv("WORKER_TIMEOUT_SECONDS", "45"))
IDLE_TIMEOUT_MIN = int(os.getenv("IDLE_TIMEOUT_MINUTES", "30"))  # 0 turns idle shutdown off

USER_TOKEN_TTL_S = 12 * 3600
LINK_TOKEN_TTL_S = 5 * 60
PASSWORD_ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
WORKER_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
STATIC_DIR = Path(__file__).parent / "static"
PLACEMENT_LOCK = 5254  # advisory lock ids; 5253 guards the schema migration
SWEEP_LOCK = 5255
ACTIVE_STATES = ("PENDING", "RUNNING", "STOPPING")  # states that use a slot on a worker

publisher = pubsub_v1.PublisherClient(
    publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
)
TOPIC_PATH = publisher.topic_path(GCP_PROJECT, PUBSUB_TOPIC)


# Writes one structured JSON log line.
def log(message: str, severity: str = "INFO", **fields) -> None:
    """One structured log line. On Cloud Run each line becomes a Cloud Logging entry whose
    jsonPayload holds these fields, with `severity` and `message` recognized. Every line
    about a workspace has `workspace_id`, so one query follows it through the API, Pub/Sub
    and the worker (the agent and proxy log the same fields)."""
    print(json.dumps({"severity": severity, "message": message, "component": "api", **fields}, default=str),
          flush=True)


# Creates the Pub/Sub topic in the local emulator.
def create_emulator_topic() -> None:
    """Locally the Pub/Sub emulator starts empty. On GCP, scripts/setup.sh creates the topic.
    Each agent creates its own filtered subscription."""
    try:
        publisher.create_topic(name=TOPIC_PATH)
    except AlreadyExists:
        pass


# Runs at API startup: creates the tables, and the emulator topic when local.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    if os.getenv("PUBSUB_EMULATOR_HOST"):
        create_emulator_topic()
    yield


app = FastAPI(title="CloudSmith API", lifespan=lifespan)


# --------------------------------------------------------------------------- auth


class Credentials(BaseModel):
    username: str
    password: str


# Hashes a password with salted PBKDF2-SHA256.
def hash_password(password: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PASSWORD_ITERATIONS).hex()
    return f"{salt}${digest}"


# Checks a password against its stored hash.
def check_password(password: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


# Signs a token with the given claims and lifetime.
def make_token(claims: dict, ttl_s: int) -> str:
    now = int(time.time())
    return jwt.encode({**claims, "iat": now, "exp": now + ttl_s}, TOKEN_SECRET, algorithm="HS256")


# Builds the sign-in reply: a session token and the user.
def session(user: dict) -> dict:
    user = {"id": str(user["id"]), "username": user["username"]}
    return {"token": make_token({"typ": "user", "sub": user["id"], "name": user["username"]}, USER_TOKEN_TTL_S),
            "user": user}


# Returns the signed-in user from the Authorization header, or refuses the request.
def current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in")
    try:
        claims = jwt.decode(authorization[7:].strip(), TOKEN_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Session expired; sign in again")
    if claims.get("typ") != "user":
        raise HTTPException(401, "Not signed in")
    return {"id": claims["sub"], "username": claims["name"]}


# Refuses internal requests without the internal token.
def check_internal(token: Optional[str]) -> None:
    if not hmac.compare_digest(token or "", INTERNAL_TOKEN):
        raise HTTPException(403, "Forbidden")


# POST /api/signup: creates an account and signs it in.
@app.post("/api/signup")
def signup(body: Credentials):
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-up is disabled")
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Username must be 3-32 characters: letters, digits, _ or -")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user = db.fetchone(
        """INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)
           ON CONFLICT (username) DO NOTHING RETURNING id, username""",
        (str(uuid.uuid4()), username, hash_password(body.password)),
    )
    if not user:
        raise HTTPException(409, "That username is taken")
    return session(user)


# POST /api/login: signs a user in.
@app.post("/api/login")
def login(body: Credentials):
    user = db.fetchone("SELECT * FROM users WHERE username = %s", (body.username.strip().lower(),))
    if not user or not check_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    return session(user)


# GET /api/me: returns the signed-in user.
@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


# --------------------------------------------------------------------- placement


# Chooses a live worker with a free slot for a workspace.
def place(conn, preferred: Optional[str] = None) -> Optional[str]:
    """Choose a live worker for a workspace inside the caller's transaction.

    The workspace's previous worker wins if it is still up (it may hold the container of a
    workspace whose stop failed). Otherwise the live worker with the most free slots is
    chosen. Returns None when every live worker is full. The advisory lock makes concurrent
    placements take turns, so two of them cannot both take a worker's last slot.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLACEMENT_LOCK,))
    fresh = "w.state = 'UP' AND w.last_heartbeat > now() - make_interval(secs => %s)"
    if preferred:
        row = conn.execute(f"SELECT w.id FROM workers w WHERE w.id = %s AND {fresh}",
                           (preferred, WORKER_TIMEOUT_S)).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        f"""SELECT w.id, w.capacity - count(ws.id) AS free
            FROM workers w
            LEFT JOIN workspaces ws ON ws.worker_id = w.id AND ws.state = ANY(%s)
            WHERE {fresh}
            GROUP BY w.id, w.capacity
            HAVING w.capacity - count(ws.id) > 0
            ORDER BY free DESC, w.id
            LIMIT 1""",
        (list(ACTIVE_STATES), WORKER_TIMEOUT_S),
    ).fetchone()
    return row["id"] if row else None


# Picks any live worker, for jobs not tied to one.
def any_live_worker() -> Optional[str]:
    row = db.fetchone(
        """SELECT id FROM workers WHERE state = 'UP' AND last_heartbeat > now() - make_interval(secs => %s)
           ORDER BY random() LIMIT 1""",
        (WORKER_TIMEOUT_S,),
    )
    return row["id"] if row else None


# --------------------------------------------------------------------- workspaces


class CreateWorkspace(BaseModel):
    name: str


# Converts a workspace row to what the dashboard shows.
def view(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "state": row["state"],
        "error": row["error"],
        "worker": row["worker_id"],
        "worker_vm": row.get("worker_vm"),  # only from list_workspaces, which joins the workers
        "created_at": row["created_at"],
        "started_at": row.get("started_at") if row["state"] == "RUNNING" else None,
    }


# Returns the user's workspace, or 404 if it does not exist or is someone else's.
def owned_workspace(ws_id: str, user: dict) -> dict:
    try:
        ws_id = str(uuid.UUID(ws_id))
    except ValueError:
        raise HTTPException(404, "Workspace not found")
    row = db.fetchone("SELECT * FROM workspaces WHERE id = %s AND owner_id = %s", (ws_id, user["id"]))
    if not row:
        raise HTTPException(404, "Workspace not found")
    return row


# Publishes a job for one worker's agent.
def send_job(action: str, ws_id: str, worker_id: str) -> None:
    data = json.dumps({"action": action, "workspace_id": ws_id}).encode()
    try:
        message_id = publisher.publish(TOPIC_PATH, data, ordering_key=ws_id, worker=worker_id).result(timeout=30)
    except Exception as exc:
        # After a failure Pub/Sub pauses this ordering key until it is resumed.
        publisher.resume_publish(TOPIC_PATH, ws_id)
        log("publish failed", "ERROR", action=action, workspace_id=ws_id, worker=worker_id, error=repr(exc))
        raise HTTPException(503, "Could not queue the request; try again")
    log("job sent", action=action, workspace_id=ws_id, worker=worker_id, message_id=message_id)


# Sets a workspace's state and error.
def set_state(ws_id: str, state: str, error: str = None) -> None:
    db.execute("UPDATE workspaces SET state = %s, error = %s, updated_at = now() WHERE id = %s", (state, error, ws_id))


NO_CAPACITY = "Every worker is full right now; try again in a few minutes"


# GET /api/workspaces: lists the user's workspaces.
@app.get("/api/workspaces")
def list_workspaces(user=Depends(current_user)):
    rows = db.fetchall(
        """SELECT ws.*, w.vm_name AS worker_vm FROM workspaces ws
           LEFT JOIN workers w ON w.id = ws.worker_id
           WHERE ws.owner_id = %s ORDER BY ws.created_at DESC""",
        (user["id"],),
    )
    return {"workspaces": [view(r) for r in rows]}


# Extracts a worker's address from its workspace URL, for the dashboard.
def worker_address(workspace_url: str) -> str:
    """Where a worker serves workspaces, for the dashboard: http://ws-{id}.34.1.2.3.sslip.io/ ->
    34.1.2.3, and http://ws-{id}.localhost:8080/ -> localhost:8080."""
    netloc = urlsplit(workspace_url.replace("{id}", "x")).netloc
    host = netloc.split(".", 1)[1] if netloc.startswith("ws-x.") else netloc
    return host.removesuffix(".sslip.io")


# A worker that has missed this many seconds of heartbeats (sent every 10 s) is shown as not
# responding; after WORKER_TIMEOUT_S it is offline and its workspaces are moved.
WORKER_LATE_S = 25


# GET /api/workers: the worker pool with each worker's status, address and load.
@app.get("/api/workers")
def list_workers(user=Depends(current_user)):
    rows = db.fetchall(
        """SELECT w.id, w.vm_name, w.workspace_url, w.capacity, w.state,
                  EXTRACT(EPOCH FROM now() - w.last_heartbeat)::float8 AS silent_s,
                  count(ws.id) FILTER (WHERE ws.state = ANY(%s)) AS used
           FROM workers w LEFT JOIN workspaces ws ON ws.worker_id = w.id
           GROUP BY w.id ORDER BY w.id""",
        (list(ACTIVE_STATES),),
    )
    workers = []
    for r in rows:
        online = r["state"] == "UP" and r["silent_s"] < WORKER_TIMEOUT_S
        status = "online" if online and r["silent_s"] < WORKER_LATE_S else "late" if online else "offline"
        workers.append({
            "id": r["id"],
            "vm": r["vm_name"] or r["id"],
            "address": worker_address(r["workspace_url"]),
            "status": status,
            "capacity": r["capacity"],
            "used": r["used"],
            "last_heartbeat_s": round(r["silent_s"]),
        })
    online = [w for w in workers if w["status"] != "offline"]
    return {
        "workers": workers,
        "online": len(online),
        "total": len(workers),
        "capacity": sum(w["capacity"] for w in online),
        "used": sum(w["used"] for w in online),
    }


# POST /api/workspaces: creates a workspace and sends its start job.
@app.post("/api/workspaces", status_code=202)
def create_workspace(body: CreateWorkspace, user=Depends(current_user)):
    name = body.name.strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(400, "Name must be 1-40 characters")
    count = db.fetchone("SELECT count(*) AS n FROM workspaces WHERE owner_id = %s", (user["id"],))["n"]
    if count >= MAX_WORKSPACES_PER_USER:
        raise HTTPException(429, f"You can have at most {MAX_WORKSPACES_PER_USER} workspaces")
    with db.transaction() as conn:
        worker = place(conn)
        if worker is None:
            raise HTTPException(503, NO_CAPACITY)
        row = conn.execute(
            """INSERT INTO workspaces (id, owner_id, name, state, worker_id)
               VALUES (%s, %s, %s, 'PENDING', %s) RETURNING *""",
            (str(uuid.uuid4()), user["id"], name, worker),
        ).fetchone()
    ws_id = str(row["id"])
    try:
        send_job("start", ws_id, worker)
    except HTTPException:
        set_state(ws_id, "FAILED", "Could not queue the request")
        raise
    return view(row)


# POST /api/workspaces/{id}/start: starts a stopped or failed workspace.
@app.post("/api/workspaces/{ws_id}/start", status_code=202)
def start_workspace(ws_id: str, user=Depends(current_user)):
    ws_id = str(owned_workspace(ws_id, user)["id"])
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id = %s FOR UPDATE", (ws_id,)).fetchone()
        if not row or row["state"] not in ("STOPPED", "FAILED"):
            raise HTTPException(409, "Only a stopped or failed workspace can be started")
        worker = place(conn, row["worker_id"])
        if worker is None:
            raise HTTPException(503, NO_CAPACITY)
        row = conn.execute(
            """UPDATE workspaces SET state = 'PENDING', error = NULL, worker_id = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (worker, ws_id),
        ).fetchone()
    try:
        send_job("start", ws_id, worker)
    except HTTPException:
        set_state(ws_id, "FAILED", "Could not queue the request")
        raise
    return view(row)


# POST /api/workspaces/{id}/stop: stops a running workspace.
@app.post("/api/workspaces/{ws_id}/stop", status_code=202)
def stop_workspace(ws_id: str, user=Depends(current_user)):
    ws_id = str(owned_workspace(ws_id, user)["id"])
    row = db.fetchone(
        """UPDATE workspaces SET state = 'STOPPING', error = NULL, updated_at = now()
           WHERE id = %s AND state = 'RUNNING' AND worker_id IS NOT NULL RETURNING *""",
        (ws_id,),
    )
    if not row:
        raise HTTPException(409, "Only a running workspace can be stopped")
    try:
        send_job("stop", ws_id, row["worker_id"])
    except HTTPException:
        set_state(ws_id, "RUNNING")
        raise
    return view(row)


# DELETE /api/workspaces/{id}: deletes a workspace and its files.
@app.delete("/api/workspaces/{ws_id}", status_code=204)
def delete_workspace(ws_id: str, user=Depends(current_user)):
    row = owned_workspace(ws_id, user)
    ws_id = str(row["id"])
    # The worker holding the container cleans up; a stopped workspace has only its archive,
    # which any worker can delete.
    worker = row["worker_id"]
    if worker is None or not db.fetchone(
        "SELECT 1 FROM workers WHERE id = %s AND state = 'UP'", (worker,)
    ):
        worker = any_live_worker()
    if worker is None:
        raise HTTPException(503, "No worker is available to delete the workspace; try again later")
    send_job("delete", ws_id, worker)  # queue first, so a failure leaves the workspace visible
    db.execute("DELETE FROM workspaces WHERE id = %s", (ws_id,))
    return Response(status_code=204)


# GET /api/workspaces/{id}/open: returns a short-lived link to the workspace.
@app.get("/api/workspaces/{ws_id}/open")
def open_workspace(ws_id: str, user=Depends(current_user)):
    """A link to the IDE on the workspace's worker, with a short-lived token. The worker's
    proxy swaps the token for a cookie."""
    row = owned_workspace(ws_id, user)
    worker = db.fetchone("SELECT workspace_url FROM workers WHERE id = %s", (row["worker_id"],)) if row["worker_id"] else None
    if row["state"] != "RUNNING" or worker is None:
        raise HTTPException(409, "The workspace is not running")
    ws_id = str(row["id"])
    token = make_token({"typ": "link", "sub": user["id"], "ws": ws_id}, LINK_TOKEN_TTL_S)
    return {"url": f"{worker['workspace_url'].format(id=ws_id)}?token={token}"}


# ----------------------------------------------------------------------- internal


class StateReport(BaseModel):
    state: str
    worker_id: str
    error: Optional[str] = None


# The states a workspace may be in when each report arrives. A report from a stale or
# duplicate job matches none of them and is ignored.
REPORT_FROM = {"RUNNING": ["PENDING"], "STOPPED": ["STOPPING"], "FAILED": ["PENDING", "STOPPING"]}


# Internal: records the state an agent reports after a job.
@app.post("/internal/workspaces/{ws_id}/state", status_code=204)
def report_state(ws_id: str, body: StateReport, x_internal_token: Optional[str] = Header(None)):
    check_internal(x_internal_token)
    if body.state not in REPORT_FROM:
        raise HTTPException(400, f"Unknown state {body.state!r}")
    try:
        ws_id = str(uuid.UUID(ws_id))
    except ValueError:
        raise HTTPException(404, "Workspace not found")
    # No-op if the workspace was deleted meanwhile, is not in an expected state, or has
    # since moved to another worker (a late report from a worker that was presumed dead).
    # A stopped workspace no longer belongs to any worker. A note set when the stop was
    # requested (such as an idle stop) is kept; a new start clears it.
    applied = db.execute(
        """UPDATE workspaces SET
             state = %(state)s,
             error = COALESCE(%(error)s, CASE WHEN %(state)s = 'STOPPED' THEN error END),
             worker_id = CASE WHEN %(state)s = 'STOPPED' THEN NULL ELSE worker_id END,
             last_active = CASE WHEN %(state)s = 'RUNNING' THEN now() ELSE last_active END,
             started_at = CASE WHEN %(state)s = 'RUNNING' THEN now() WHEN %(state)s = 'STOPPED' THEN NULL
                               ELSE started_at END,
             updated_at = now()
           WHERE id = %(id)s AND state = ANY(%(from)s) AND worker_id = %(worker)s""",
        {"state": body.state, "error": body.error, "id": ws_id,
         "from": REPORT_FROM[body.state], "worker": body.worker_id},
    )
    if body.state == "FAILED" and applied:
        log("job failed", "ERROR", workspace_id=ws_id, worker=body.worker_id, error=body.error)
    else:
        log("state reported", workspace_id=ws_id, worker=body.worker_id, state=body.state,
            applied=bool(applied))  # not applied: a stale or duplicate report, ignored
    return Response(status_code=204)


# Internal: tells an agent whether a job is still its own.
@app.get("/internal/workspaces/{ws_id}")
def workspace_status(ws_id: str, x_internal_token: Optional[str] = Header(None)):
    """Called by the agent before it runs a start or stop job, to check the job is still its
    own. A job can wait in a worker's subscription while that worker is down, and by the time
    the worker returns the workspace may have moved to another worker or been stopped."""
    check_internal(x_internal_token)
    try:
        ws_id = str(uuid.UUID(ws_id))
    except ValueError:
        raise HTTPException(404, "Workspace not found")
    row = db.fetchone("SELECT state, worker_id FROM workspaces WHERE id = %s", (ws_id,))
    if not row:
        raise HTTPException(404, "Workspace not found")
    return {"state": row["state"], "worker_id": row["worker_id"]}


class Heartbeat(BaseModel):
    workspace_url: str                 # e.g. http://ws-{id}.10.0.0.5.sslip.io/
    capacity: int
    activity: Dict[str, float] = {}    # workspace id -> unix time a browser last used it
    vm: str = ""                       # the worker's VM name, e.g. cloudsmith-worker-1 (for the dashboard)


# Internal: receives a worker's heartbeat.
@app.post("/internal/workers/{worker_id}/heartbeat")
def heartbeat(worker_id: str, body: Heartbeat, x_internal_token: Optional[str] = Header(None)):
    """Called by each agent every few seconds. Registers the worker, records workspace
    activity, runs the dead-worker and idle checks, and tells the worker which workspaces
    it owns (so it can stop containers that were moved elsewhere while it was unreachable)."""
    check_internal(x_internal_token)
    if not WORKER_ID_RE.match(worker_id) or "{id}" not in body.workspace_url or body.capacity < 0:
        raise HTTPException(400, "Bad heartbeat")
    db.execute(
        """INSERT INTO workers (id, workspace_url, capacity, state, last_heartbeat, vm_name)
           VALUES (%s, %s, %s, 'UP', now(), NULLIF(%s, ''))
           ON CONFLICT (id) DO UPDATE SET workspace_url = EXCLUDED.workspace_url,
             capacity = EXCLUDED.capacity, state = 'UP', last_heartbeat = now(), vm_name = EXCLUDED.vm_name""",
        (worker_id, body.workspace_url, body.capacity, body.vm[:64]),
    )
    record_activity(worker_id, body.activity)
    sweep()
    rows = db.fetchall("SELECT id FROM workspaces WHERE worker_id = %s", (worker_id,))
    # Read by the cloudsmith_workspaces_per_worker log-based metric (monitoring/).
    log("worker heartbeat", worker=worker_id, workspaces=len(rows), capacity=body.capacity)
    return {"assigned": [str(r["id"]) for r in rows]}


# Records when each workspace on a worker was last used.
def record_activity(worker_id: str, activity: Dict[str, float]) -> None:
    ids, times = [], []
    for ws_id, ts in activity.items():
        try:
            ids.append(str(uuid.UUID(ws_id)))
            times.append(float(ts))
        except (ValueError, TypeError):
            continue
    if ids:
        db.execute(
            """UPDATE workspaces w SET last_active = GREATEST(w.last_active, to_timestamp(a.t))
               FROM unnest(%s::uuid[], %s::float8[]) AS a(id, t)
               WHERE w.id = a.id AND w.worker_id = %s""",
            (ids, times, worker_id),
        )


# Recovers workspaces from dead workers and stops idle ones.
def sweep() -> None:
    """Handle workers that stopped sending heartbeats, and workspaces left idle.

    Runs on every heartbeat; the advisory lock makes concurrent heartbeats skip it rather
    than wait. Workspaces on a dead worker are restarted on another worker from their last
    archive (changes made since they last started are lost with the worker), or marked
    stopped when there is no room. Idle workspaces get a stop job like a user's Stop.
    """
    jobs = []
    with db.transaction() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (SWEEP_LOCK,)).fetchone()["ok"]:
            return
        for w in conn.execute(
            """UPDATE workers SET state = 'DOWN'
               WHERE state = 'UP' AND last_heartbeat < now() - make_interval(secs => %s) RETURNING id""",
            (WORKER_TIMEOUT_S,),
        ).fetchall():
            log("worker stopped sending heartbeats", "WARNING", worker=w["id"])

        lost = conn.execute(
            """SELECT id, state FROM workspaces
               WHERE worker_id IN (SELECT id FROM workers WHERE state = 'DOWN') FOR UPDATE""",
        ).fetchall()
        for ws in lost:
            target = place(conn) if ws["state"] in ("PENDING", "RUNNING") else None
            if target:
                conn.execute(
                    """UPDATE workspaces SET state = 'PENDING', worker_id = %s, updated_at = now(),
                         error = 'Its worker stopped responding; restarting it on another worker'
                       WHERE id = %s""",
                    (target, ws["id"]),
                )
                jobs.append(("start", str(ws["id"]), target))
            else:
                conn.execute(
                    """UPDATE workspaces SET state = 'STOPPED', worker_id = NULL, updated_at = now(),
                         error = 'Its worker stopped responding; start it again (recent changes may be lost)'
                       WHERE id = %s""",
                    (ws["id"],),
                )
            log("recovered workspace from a dead worker", "WARNING", workspace_id=ws["id"], was=ws["state"],
                moved_to=target)

        if IDLE_TIMEOUT_MIN > 0:
            for ws in conn.execute(
                """UPDATE workspaces SET state = 'STOPPING', updated_at = now(),
                     error = %s
                   WHERE state = 'RUNNING'
                     AND COALESCE(last_active, updated_at) < now() - make_interval(mins => %s)
                     AND worker_id IN (SELECT id FROM workers WHERE state = 'UP')
                   RETURNING id, worker_id""",
                (f"Stopped automatically after {IDLE_TIMEOUT_MIN} minute{'s' * (IDLE_TIMEOUT_MIN != 1)} without use",
                 IDLE_TIMEOUT_MIN),
            ).fetchall():
                log("stopping idle workspace", workspace_id=ws["id"], worker=ws["worker_id"])
                jobs.append(("stop", str(ws["id"]), ws["worker_id"]))

    for action, ws_id, worker in jobs:  # publish after the transaction has committed
        try:
            send_job(action, ws_id, worker)
        except HTTPException:
            if action == "stop":
                set_state(ws_id, "RUNNING")  # try again on a later sweep
            else:
                set_state(ws_id, "FAILED", "Could not queue the restart")


# Health check: confirms the database answers.
@app.get("/healthz")
def healthz():
    db.fetchone("SELECT 1")
    return {"status": "ok"}


# Serves the dashboard page.
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
