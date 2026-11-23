"""The agent's jobs against real Docker and a Cloud Storage emulator: each job does its work,
is safe to run twice, and leaves nothing half-done behind when it fails."""
import time
import uuid
from datetime import datetime, timezone

import agent
import pytest
from docker.errors import ImageNotFound, NotFound


# Returns the workspace's container, or None.
def container(ws_id):
    try:
        return agent.client.containers.get(f"ws-{ws_id}")
    except NotFound:
        return None


# Whether a Docker object with this name exists.
def exists(collection, name):
    try:
        collection.get(name)
        return True
    except NotFound:
        return False


# Runs a shell command in the workspace as its user and returns the output.
def run(ws_id, command):
    code, out = container(ws_id).exec_run(["sh", "-c", command], user="coder")
    assert code == 0, out.decode()
    return out.decode()


# Writes a project file and a hidden settings file into the workspace.
def write_files(ws_id):
    run(ws_id, "mkdir -p ~/project/src ~/.config/app && echo 'print(42)' > ~/project/src/main.py"
               " && echo theme=dark > ~/.config/app/settings")


# Checks the files from write_files are there and owned by the workspace user.
def assert_files(ws_id):
    assert run(ws_id, "cat ~/project/src/main.py") == "print(42)\n"
    assert run(ws_id, "cat ~/.config/app/settings") == "theme=dark\n"  # hidden settings too
    assert run(ws_id, "stat -c %U ~/project/src/main.py") == "coder\n"
    run(ws_id, "touch ~/project/src/still-writable")


# Whether the workspace's container, network and volume are all gone.
def gone(ws_id):
    return not (container(ws_id) or exists(agent.client.networks, f"wsnet-{ws_id}")
                or exists(agent.client.volumes, f"wsvol-{ws_id}"))


# --------------------------------------------------------------------------- start


# Start creates a workspace container with its limits and isolation settings.
def test_start_creates_an_isolated_workspace(ws_id):
    agent.start(ws_id)
    c = container(ws_id)
    assert c.status == "running"
    assert c.labels == {agent.LABEL: ws_id, agent.WORKER_LABEL: "test-worker"}

    host_config = c.attrs["HostConfig"]
    assert host_config["Memory"] == 1024 * 1024 * 1024
    assert host_config["NanoCpus"] == 1_000_000_000
    assert host_config["PidsLimit"] == 512
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["RestartPolicy"]["Name"] == "unless-stopped"
    assert run(ws_id, "id -u") == "1000\n"  # not root

    assert f"VSCODE_PROXY_URI=http://{{{{port}}}}-ws-{ws_id}.worker.test/" in c.attrs["Config"]["Env"]
    mounts = {m["Destination"]: m["Name"] for m in c.attrs["Mounts"]}
    assert mounts == {"/home/coder": f"wsvol-{ws_id}"}

    # Its network holds only the workspace and this worker's proxy.
    network = agent.client.networks.get(f"wsnet-{ws_id}")
    names = {info["Name"] for info in network.attrs["Containers"].values()}
    assert names == {f"ws-{ws_id}", agent.PROXY_CONTAINER}


# Starting a workspace twice is harmless.
def test_start_twice_is_harmless(ws_id):
    agent.start(ws_id)
    first = container(ws_id).id
    agent.start(ws_id)
    assert container(ws_id).id == first and container(ws_id).status == "running"
    assert len(agent.client.containers.list(filters={"label": f"{agent.LABEL}={ws_id}"})) == 1


# Start reuses a container left by a stop whose archive failed.
def test_start_reuses_a_container_that_is_still_there(ws_id):
    agent.start(ws_id)
    write_files(ws_id)
    container(ws_id).stop(timeout=5)
    agent.start(ws_id)
    assert_files(ws_id)


# Start reuses a container that is newer than the workspace's archive.
def test_start_reuses_a_container_newer_than_its_archive(ws_id):
    """A stop whose archive upload failed, after an earlier stop that did archive: the container
    was created after the archive, so it holds the newest files and is started again."""
    agent.start(ws_id)
    run(ws_id, "mkdir -p ~/project && echo archived > ~/project/f.txt")
    agent.stop(ws_id)
    agent.start(ws_id)
    run(ws_id, "echo newer > ~/project/f.txt")
    container(ws_id).stop(timeout=5)  # its archive upload "failed": the container is kept
    first = container(ws_id).id
    agent.start(ws_id)
    assert container(ws_id).id == first
    assert run(ws_id, "cat ~/project/f.txt") == "newer\n"


# Start restores a newer archive instead of reusing an older container.
def test_start_prefers_a_newer_archive_to_an_old_container(ws_id):
    """The workspace moved to another worker while this one was presumed dead, so this
    worker's copy was only stopped. The other worker then archived newer files. Starting the
    workspace here again must restore that archive, not restart the old copy."""
    agent.start(ws_id)
    run(ws_id, "mkdir -p ~/project && echo old > ~/project/f.txt")
    container(ws_id).stop(timeout=5)  # what stop_unowned does to it
    old = container(ws_id).id

    # The other worker's copy, archived after this one was created.
    elsewhere = str(uuid.uuid4())
    try:
        agent.start(elsewhere)
        run(elsewhere, "mkdir -p ~/project && echo newer > ~/project/f.txt")
        container(elsewhere).stop(timeout=5)
        agent.archive(container(elsewhere), ws_id)
    finally:
        agent.remove_workspace(elsewhere)

    agent.start(ws_id)
    assert container(ws_id).id != old
    assert run(ws_id, "cat ~/project/f.txt") == "newer\n"


# A failed start leaves no container, network or volume behind.
def test_a_failed_start_leaves_nothing_behind(ws_id, monkeypatch):
    agent.start(ws_id)
    write_files(ws_id)
    agent.stop(ws_id)
    monkeypatch.setattr(agent, "WORKSPACE_IMAGE", "cloudsmith/no-such-image:latest")
    with pytest.raises(ImageNotFound):
        agent.start(ws_id)
    assert gone(ws_id)
    assert agent.archive_blob(ws_id).exists()  # the files are untouched in Cloud Storage

    monkeypatch.undo()
    agent.start(ws_id)  # a retry works
    assert_files(ws_id)


# Start replaces a container left by another worker with one restored from the archive.
def test_start_replaces_a_container_left_by_another_worker(ws_id, monkeypatch):
    """Workers that share one Docker engine (the local setup): when a workspace moves here from
    a worker presumed dead, it starts from its archive, as it would on a separate VM."""
    agent.start(ws_id)
    write_files(ws_id)
    agent.stop(ws_id)

    monkeypatch.setattr(agent, "WORKER_ID", "test-other-worker")
    agent.start(ws_id)
    run(ws_id, "echo unsaved > ~/project/not-archived.txt")
    monkeypatch.undo()

    agent.start(ws_id)
    assert container(ws_id).labels[agent.WORKER_LABEL] == "test-worker"
    assert_files(ws_id)
    assert run(ws_id, "ls ~/project") == "src\n"  # the other worker's unsaved change is gone


# ---------------------------------------------------------------------------- stop


# Stop archives the files and removes the container, network and volume.
def test_stop_archives_the_files_and_removes_the_workspace(ws_id):
    agent.start(ws_id)
    write_files(ws_id)
    agent.stop(ws_id)
    assert agent.archive_blob(ws_id).exists()
    assert gone(ws_id)
    agent.stop(ws_id)  # delivered twice: nothing left to do


# Files survive a stop and a start.
def test_files_survive_stop_and_start(ws_id):
    agent.start(ws_id)
    write_files(ws_id)
    agent.stop(ws_id)
    agent.start(ws_id)
    assert_files(ws_id)


# A second stop and start keeps the latest files.
def test_a_second_stop_and_start_keeps_the_latest_files(ws_id):
    agent.start(ws_id)
    write_files(ws_id)
    agent.stop(ws_id)
    agent.start(ws_id)
    run(ws_id, "echo 'print(43)' > ~/project/src/main.py && rm ~/.config/app/settings")
    agent.stop(ws_id)
    agent.start(ws_id)
    assert run(ws_id, "cat ~/project/src/main.py") == "print(43)\n"
    assert run(ws_id, "ls ~/.config/app") == ""


# -------------------------------------------------------------------------- delete


# Delete removes the workspace and its archive.
def test_delete_removes_the_workspace_and_its_archive(ws_id):
    agent.start(ws_id)
    write_files(ws_id)
    agent.stop(ws_id)
    agent.start(ws_id)
    agent.delete(ws_id)
    assert gone(ws_id)
    assert not agent.archive_blob(ws_id).exists()
    agent.delete(ws_id)  # delivered twice: nothing left to do


# Deleting a workspace that never started is harmless.
def test_delete_of_a_workspace_that_never_started_is_harmless(ws_id):
    agent.delete(ws_id)
    assert gone(ws_id)


# ----------------------------------------------------- containers it no longer owns


# Running containers the API has moved to another worker are stopped.
def test_running_containers_the_api_moved_elsewhere_are_stopped(ws_id, monkeypatch):
    other_ws, mine = ws_id, str(uuid.uuid4())
    try:
        agent.start(mine)
        agent.start(other_ws)

        agent.stop_unowned({mine})  # both just started: within the grace period, nothing is stopped
        assert container(other_ws).status == "running"

        monkeypatch.setattr(agent, "started_seconds_ago", lambda c: 1000)
        agent.stop_unowned({mine})
        container(other_ws).reload()
        assert container(other_ws).status == "exited"
        assert exists(agent.client.volumes, f"wsvol-{other_ws}")  # only stopped, never deleted
        assert container(mine).status == "running"
    finally:
        agent.remove_workspace(mine)


# started_seconds_ago reads the container's start time, and is 0 when it has none.
def test_started_seconds_ago():
    class FakeContainer:
        attrs = {"State": {"StartedAt": "2026-11-11T10:00:00.123456789Z"}}
    started = datetime(2026, 11, 11, 10, tzinfo=timezone.utc).timestamp()
    assert abs(agent.started_seconds_ago(FakeContainer()) - (time.time() - started)) < 5

    FakeContainer.attrs = {"State": {}}
    assert agent.started_seconds_ago(FakeContainer()) == 0.0


# ports_url builds the address template for apps on ports.
def test_ports_url():
    assert agent.ports_url("abc") == "http://{{port}}-ws-abc.worker.test/"
