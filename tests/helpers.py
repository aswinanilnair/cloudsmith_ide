"""Shared helpers for the component tests: throwaway Docker containers on free ports."""
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Returns a free local TCP port.
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Whether a Docker engine is reachable.
def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# Points the Docker SDK at the Docker CLI's current engine.
def set_docker_host() -> None:
    """Point the Docker SDK at the CLI's current engine. Docker Desktop on macOS may not
    create /var/run/docker.sock, which the SDK uses by default."""
    if os.getenv("DOCKER_HOST"):
        return
    result = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                            capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        os.environ["DOCKER_HOST"] = result.stdout.strip()


# Starts a detached container, replacing a leftover with the same name.
def run_container(name: str, *args: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, *args], check=True, capture_output=True)


# Removes a container if it exists.
def remove_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# Waits until a check passes, or fails after the timeout.
def wait_until(check, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"{what} was not ready after {timeout:.0f} s")


# Whether a URL answers without a server error.
def http_ok(url: str) -> bool:
    with urllib.request.urlopen(url, timeout=2) as resp:
        return resp.status < 500


# Parses the structured (JSON) log lines in captured output.
def log_lines(output: str) -> list:
    lines = []
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "message" in entry:
            lines.append(entry)
    return lines


# Returns the log lines with the given message.
def find_log(output: str, message: str) -> list:
    return [entry for entry in log_lines(output) if entry["message"] == message]
