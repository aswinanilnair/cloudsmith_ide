"""CloudSmith agent: runs on each worker VM and is the only thing that talks to Docker.

It pulls this worker's jobs from Pub/Sub, runs them, and reports the new state to the API:

  start   create the workspace's network, volume and container, restore its files from
          Cloud Storage, then start it
  stop    stop the container, archive the workspace's home folder to Cloud Storage, then
          remove the container, network and volume (the archive is now the only copy)
  delete  remove the container, network, volume and archive

Every job is safe to run twice, because Pub/Sub may deliver a message more than once.
Before a start or stop, the agent asks the API whether the job is still this worker's: a job
can wait in this worker's subscription while the worker is down, and meanwhile the API may
have moved the workspace to another worker. Such a stale job is skipped.

Every HEARTBEAT_SECONDS it also sends the API a heartbeat with this worker's capacity and
when each workspace was last used (from the proxy). The reply lists the workspaces this
worker owns; a running workspace container that is not on the list was moved to another
worker while this one was unreachable, so it is stopped.

Each workspace gets its own Docker network that only it and this worker's proxy join, so
one user's workspace cannot reach another's (code-server runs without a password).
"""
import gzip
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

import docker
import requests
from docker.errors import APIError, NotFound
from google.api_core.exceptions import AlreadyExists
from google.api_core.exceptions import NotFound as GcsNotFound
from google.auth.credentials import AnonymousCredentials
from google.cloud import pubsub_v1, storage

WORKER_ID = os.getenv("WORKER_ID", "worker-1")
WORKER_VM = os.getenv("WORKER_VM", "")  # shown on the dashboard, e.g. cloudsmith-worker-1
GCP_PROJECT = os.getenv("GCP_PROJECT", "local-project")
PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "workspace-jobs")
PUBSUB_SUBSCRIPTION = os.getenv("PUBSUB_SUBSCRIPTION", f"workspace-jobs-{WORKER_ID}")
GCS_BUCKET = os.getenv("GCS_BUCKET", "cloudsmith-workspaces")
API_URL = os.getenv("API_URL", "http://api:8000").rstrip("/")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "dev-internal-token")
WORKSPACE_IMAGE = os.getenv("WORKSPACE_IMAGE", "cloudsmith/workspace:latest")
WORKSPACE_URL = os.getenv("WORKSPACE_URL", "http://ws-{id}.localhost:8080/")
PROXY_CONTAINER = os.getenv("PROXY_CONTAINER", "cloudsmith-proxy")
PROXY_ACTIVITY_URL = os.getenv("PROXY_ACTIVITY_URL", f"http://{PROXY_CONTAINER}:8080/_cloudsmith/activity")
CAPACITY = int(os.getenv("WORKER_CAPACITY", "6"))
CPU_LIMIT = float(os.getenv("WS_CPU_LIMIT", "1.0"))
MEM_LIMIT_MB = int(os.getenv("WS_MEM_LIMIT_MB", "1024"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "4"))
HEARTBEAT_S = int(os.getenv("HEARTBEAT_SECONDS", "10"))
ORPHAN_GRACE_S = 120  # never stop a container younger than this: its job may be newer than the heartbeat reply

LABEL = "cloudsmith.workspace"
WORKER_LABEL = "cloudsmith.worker"
HOME_DIR = "/home/coder"  # the whole home folder is kept, so settings and installed packages survive

client = docker.from_env()
if os.getenv("STORAGE_EMULATOR_HOST"):
    gcs = storage.Client(project=GCP_PROJECT, credentials=AnonymousCredentials())
else:
    gcs = storage.Client(project=GCP_PROJECT)
bucket = gcs.bucket(GCS_BUCKET)
# Held while remove_workspace() detaches the proxy and removes a network, so that
# reconnect_proxy_forever cannot re-attach the proxy in between.
proxy_lock = threading.Lock()


# Writes one structured JSON log line.
def log(message: str, severity: str = "INFO", **fields) -> None:
    """One structured log line, in the same format as the API's. On GCP the VM's logging
    agent sends it to Cloud Logging; `workspace_id` ties it to the API's lines for the same
    workspace."""
    print(json.dumps({"severity": severity, "message": message, "component": "agent", "worker": WORKER_ID,
                      **fields}, default=str), flush=True)


# Returns a Docker object by name, or None if it does not exist.
def find(collection, name: str):
    try:
        return collection.get(name)
    except NotFound:
        return None


# Attaches the proxy container to a workspace network (if not already attached).
def connect_proxy(network) -> None:
    try:
        network.connect(PROXY_CONTAINER)
    except APIError as exc:
        if "already exists" not in str(exc):
            raise


# The Cloud Storage object that holds a workspace's archive.
def archive_blob(ws_id: str):
    return bucket.blob(f"workspaces/{ws_id}.tar.gz")


# When the workspace's archive was last written, or None if it has none.
def archive_time(ws_id: str):
    blob = bucket.get_blob(f"workspaces/{ws_id}.tar.gz")
    return blob.updated.timestamp() if blob is not None and blob.updated else None


# Converts a Docker timestamp to unix time, to the microsecond.
def docker_time(value: str) -> float:
    """Unix time of a Docker timestamp such as 2026-11-11T10:00:00.123456789Z (0 if unset),
    to the microsecond: a start right after a stop must still count as newer than its archive."""
    whole, _, rest = value.rstrip("Z").partition(".")
    try:
        seconds = datetime.fromisoformat(whole).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0
    digits = "".join(ch for ch in rest if ch.isdigit())[:6]
    return seconds + (int(digits.ljust(6, "0")) / 1e6 if digits else 0.0)


# The address template for apps on ports, used by code-server's Ports tab.
def ports_url(ws_id: str) -> str:
    """Where code-server's Ports tab opens an app on a port: http://{{port}}-ws-<id>.<domain>/,
    which this worker's proxy routes to that port inside the workspace."""
    return WORKSPACE_URL.format(id=ws_id).replace("://ws-", "://{{port}}-ws-", 1)


# ----------------------------------------------------------------------- archives


# Archives a stopped container's home folder to Cloud Storage.
def archive(container, ws_id: str) -> None:
    """Copy the (stopped) container's home folder to Cloud Storage as a .tar.gz.

    The archive is written to a temporary file first and then uploaded. Cloud Storage
    replaces the object only once an upload completes, so a failed upload leaves the
    previous archive in place.
    """
    stream, _ = container.get_archive(HOME_DIR)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with gzip.GzipFile(fileobj=tmp, mode="wb", compresslevel=6) as gz:
            for chunk in stream:
                gz.write(chunk)
        tmp.flush()
        size = tmp.tell()
        archive_blob(ws_id).upload_from_filename(tmp.name, content_type="application/gzip")
    log("archived workspace", workspace_id=ws_id, bytes=size)


# Restores a workspace's archive into a new container before it starts.
def restore(container, ws_id: str) -> bool:
    """Unpack the workspace's archive into a created (not yet started) container.
    Returns False for a new workspace, which has no archive yet."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        try:
            archive_blob(ws_id).download_to_filename(tmp.name)
        except GcsNotFound:
            return False
        with open(tmp.name, "rb") as data:
            # The archive holds coder/..., so unpacking it in /home recreates /home/coder.
            # Docker accepts a gzip-compressed tar here directly.
            container.put_archive("/home", data)
        log("restored workspace", workspace_id=ws_id, bytes=os.path.getsize(tmp.name))
    return True


# --------------------------------------------------------------------------- jobs


# Removes a workspace's container, network and volume from this worker.
def remove_workspace(ws_id: str) -> None:
    container = find(client.containers, f"ws-{ws_id}")
    if container is not None:
        container.remove(force=True)
    network = find(client.networks, f"wsnet-{ws_id}")
    if network is not None:
        with proxy_lock:
            network.reload()
            for attached in network.attrs.get("Containers") or {}:  # this worker's proxy, normally
                try:
                    network.disconnect(attached, force=True)
                except APIError:
                    pass
            network.remove()
    volume = find(client.volumes, f"wsvol-{ws_id}")
    if volume is not None:
        volume.remove(force=True)


# Start job: creates (or reuses) the workspace container, restores its files and starts it.
def start(ws_id: str) -> None:
    container = find(client.containers, f"ws-{ws_id}")
    if container is not None and container.labels.get(WORKER_LABEL) != WORKER_ID:
        # Left by another worker on the same Docker engine, which only happens when workers
        # share one (as in the local Compose setup). The workspace was moved here because
        # that worker was presumed dead, so start from the archive, as a separate VM would.
        log("replacing a container left by another worker", "WARNING", workspace_id=ws_id,
            other=container.labels.get(WORKER_LABEL))
        remove_workspace(ws_id)
        container = None
    elif container is not None:
        archived = archive_time(ws_id)
        if archived is not None and archived > docker_time(container.attrs.get("Created", "")):
            # Left here when the workspace moved to another worker, which has since archived
            # newer files (e.g. this worker was presumed dead, and its old copy was only
            # stopped). Start from the archive instead of the old copy.
            log("replacing an old container with the newer archive", "WARNING", workspace_id=ws_id)
            remove_workspace(ws_id)
            container = None
    if container is not None:
        # Still here from an earlier start, or from a stop whose archive failed (then this
        # container holds the newest files, created after the last archive): start it again.
        network = find(client.networks, f"wsnet-{ws_id}")
        if network is not None:
            connect_proxy(network)
        container.start()  # no-op if it is already running
        return

    labels = {LABEL: ws_id, WORKER_LABEL: WORKER_ID}
    try:
        network = find(client.networks, f"wsnet-{ws_id}") or client.networks.create(f"wsnet-{ws_id}", labels=labels)
        connect_proxy(network)
        client.volumes.create(f"wsvol-{ws_id}", labels=labels)  # returns the existing volume if there is one
        container = client.containers.create(
            WORKSPACE_IMAGE,
            name=f"ws-{ws_id}",  # the proxy reaches it at http://ws-<id>:8080
            hostname="workspace",
            labels=labels,
            environment={"VSCODE_PROXY_URI": ports_url(ws_id)},
            network=network.name,
            volumes={f"wsvol-{ws_id}": {"bind": HOME_DIR, "mode": "rw"}},
            # Isolation: resource limits, non-root user (set in the image), no extra privileges.
            mem_limit=f"{MEM_LIMIT_MB}m",
            nano_cpus=int(CPU_LIMIT * 1e9),
            pids_limit=512,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            restart_policy={"Name": "unless-stopped"},  # comes back after a VM reboot
        )
        restore(container, ws_id)
        container.start()
    except Exception:
        # Leave nothing half-restored behind; the archive in Cloud Storage is untouched.
        remove_workspace(ws_id)
        raise


# Stop job: archives the workspace's files, then removes its container, network and volume.
def stop(ws_id: str) -> None:
    container = find(client.containers, f"ws-{ws_id}")
    if container is None:
        return  # already archived and removed by an earlier delivery of this job
    container.stop(timeout=10)
    archive(container, ws_id)
    remove_workspace(ws_id)


# Delete job: removes the workspace and its archive.
def delete(ws_id: str) -> None:
    remove_workspace(ws_id)
    try:
        archive_blob(ws_id).delete()
    except GcsNotFound:
        pass


ACTIONS = {"start": start, "stop": stop, "delete": delete}
STATE_AFTER = {"start": "RUNNING", "stop": "STOPPED"}  # delete has nothing to report
# The state the API puts a workspace in when it sends each job. Delete has none: the API has
# already removed the workspace, and cleaning up after it is always right.
STATE_BEFORE = {"start": "PENDING", "stop": "STOPPING"}


# Asks the API whether a start or stop job still belongs to this worker.
def still_mine(action: str, ws_id: str) -> bool:
    """Whether the API still wants this worker to run this job: the workspace is assigned to
    this worker and is in the state the job was sent for. False for a stale job, e.g. a start
    that waited here while this worker was down and the workspace was restarted elsewhere.
    Raises requests.RequestException if the API cannot answer."""
    resp = requests.get(
        f"{API_URL}/internal/workspaces/{ws_id}",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=15,
    )
    if resp.status_code == 404:
        return False  # deleted meanwhile; its delete job cleans up
    resp.raise_for_status()
    ws = resp.json()
    return ws["worker_id"] == WORKER_ID and ws["state"] == STATE_BEFORE[action]


# Whether a failed API call is worth retrying (unreachable or a 5xx error).
def retry_later(exc: requests.RequestException) -> bool:
    """Whether a failed call to the API is worth retrying: it was unreachable or failing (5xx).
    A 4xx, such as a wrong INTERNAL_TOKEN, would fail again on every retry."""
    status = exc.response.status_code if exc.response is not None else None
    return status is None or status >= 500


# Reports a workspace's new state to the API.
def report(ws_id: str, state: str, error: str = None) -> None:
    resp = requests.post(
        f"{API_URL}/internal/workspaces/{ws_id}/state",
        json={"state": state, "error": error, "worker_id": WORKER_ID},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()


# Handles one Pub/Sub job message: checks it, runs it, reports the result and acks it.
def handle(message) -> None:
    try:
        job = json.loads(message.data)
        action, ws_id = job["action"], str(uuid.UUID(job["workspace_id"]))
        run = ACTIONS[action]
    except (ValueError, KeyError, TypeError):
        log("dropping malformed job", "ERROR", data=message.data.decode(errors="replace"))
        message.ack()
        return

    log("job received", action=action, workspace_id=ws_id)
    if action in STATE_BEFORE:
        try:
            if not still_mine(action, ws_id):
                log("skipping a job that is no longer this worker's", "WARNING", action=action, workspace_id=ws_id)
                message.ack()
                return
        except requests.RequestException as exc:
            if retry_later(exc):
                log("could not check the job with the API; will retry", "WARNING", workspace_id=ws_id, error=repr(exc))
                message.nack()
                return
            log("the API rejected the check; dropping the job", "ERROR", workspace_id=ws_id, error=repr(exc))
            message.ack()
            return

    try:
        run(ws_id)
        state, error = STATE_AFTER.get(action), None
    except Exception as exc:
        log("job failed", "ERROR", action=action, workspace_id=ws_id, error=repr(exc))
        if action == "delete":
            # The API has already removed the workspace, so nobody would retry it: let
            # Pub/Sub redeliver the job until the container, network, volume and archive are gone.
            message.nack()
            return
        state, error = "FAILED", f"{action} failed: {exc}"[:500]

    try:
        if state:
            report(ws_id, state, error)
    except requests.RequestException as exc:
        if retry_later(exc):
            log("could not report to the API; will retry", "WARNING", workspace_id=ws_id, error=repr(exc))
            message.nack()
            return
        log("the API rejected the report; dropping it", "ERROR", workspace_id=ws_id, error=repr(exc))
    message.ack()
    log("job done", action=action, workspace_id=ws_id, state=state)


# ---------------------------------------------------------------------- heartbeat


# Reads when each workspace was last used from this worker's proxy.
def proxy_activity() -> dict:
    try:
        resp = requests.get(PROXY_ACTIVITY_URL, headers={"X-Internal-Token": INTERNAL_TOKEN}, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        log("could not read workspace activity from the proxy", "WARNING", error=repr(exc))
        return {}


# Seconds since the container started, or 0 if unknown.
def started_seconds_ago(container) -> float:
    started = docker_time(container.attrs.get("State", {}).get("StartedAt", ""))
    return time.time() - started if started else 0.0


# Stops running workspace containers the API has moved to another worker.
def stop_unowned(assigned: set) -> None:
    """Stop running workspace containers that the API has moved to another worker.

    That happens when this worker was unreachable long enough to be presumed dead. The
    container and volume are kept (only stopped), so nothing is deleted by mistake.
    """
    for container in client.containers.list(filters={"label": f"{WORKER_LABEL}={WORKER_ID}"}):
        ws_id = container.labels.get(LABEL)
        if ws_id in assigned or started_seconds_ago(container) < ORPHAN_GRACE_S:
            continue
        log("stopping a workspace this worker no longer owns", "WARNING", workspace_id=ws_id)
        container.stop(timeout=10)


# Sends a heartbeat to the API every few seconds and stops containers it no longer owns.
def heartbeat_forever() -> None:
    while True:
        try:
            resp = requests.post(
                f"{API_URL}/internal/workers/{WORKER_ID}/heartbeat",
                json={"workspace_url": WORKSPACE_URL, "capacity": CAPACITY, "activity": proxy_activity(),
                      "vm": WORKER_VM},
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                timeout=15,
            )
            resp.raise_for_status()
            stop_unowned(set(resp.json()["assigned"]))
        except Exception as exc:
            log("heartbeat failed", "WARNING", error=repr(exc))
        time.sleep(HEARTBEAT_S)


# --------------------------------------------------------------------------- main


# Keeps the proxy attached to every workspace network on this worker.
def reconnect_proxy_forever() -> None:
    """Re-attach the proxy to every network of this worker's workspaces. A new proxy
    container (after a deploy or a restart of the stack) starts without them."""
    while True:
        try:
            networks = client.networks.list(filters={"label": [LABEL, f"{WORKER_LABEL}={WORKER_ID}"]})
        except Exception as exc:
            log("could not list workspace networks", "WARNING", error=repr(exc))
            networks = []
        for network in networks:
            try:
                with proxy_lock:
                    connect_proxy(network)
            except NotFound:
                pass  # deleted since it was listed
            except Exception as exc:
                log("could not attach the proxy to a workspace network", "WARNING", network=network.name, error=repr(exc))
        time.sleep(15)


# Creates the Pub/Sub topic and subscription in the local emulator.
def create_emulator_resources(subscriber, subscription: str) -> None:
    """Locally the emulators start empty. On GCP, scripts/setup.sh creates these."""
    if os.getenv("PUBSUB_EMULATOR_HOST"):
        topic = subscriber.topic_path(GCP_PROJECT, PUBSUB_TOPIC)
        try:
            pubsub_v1.PublisherClient().create_topic(name=topic)
        except AlreadyExists:
            pass
        try:
            subscriber.create_subscription(request={
                "name": subscription,
                "topic": topic,
                "enable_message_ordering": True,
                "ack_deadline_seconds": 60,
                "filter": f'attributes.worker = "{WORKER_ID}"',
            })
        except AlreadyExists:
            pass
    if os.getenv("STORAGE_EMULATOR_HOST") and not bucket.exists():
        gcs.create_bucket(GCS_BUCKET)


# Starts the background threads and listens for jobs.
def main() -> None:
    subscriber = pubsub_v1.SubscriberClient()
    path = subscriber.subscription_path(GCP_PROJECT, PUBSUB_SUBSCRIPTION)
    create_emulator_resources(subscriber, path)
    threading.Thread(target=reconnect_proxy_forever, daemon=True).start()
    threading.Thread(target=heartbeat_forever, daemon=True).start()  # only after the subscription exists
    future = subscriber.subscribe(
        path, callback=handle, flow_control=pubsub_v1.types.FlowControl(max_messages=MAX_CONCURRENT_JOBS)
    )
    log("agent listening", subscription=path, image=WORKSPACE_IMAGE, bucket=GCS_BUCKET)
    future.result()  # blocks; raises (and the container restarts) if the subscription fails


if __name__ == "__main__":
    main()
