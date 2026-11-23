# Tests

```bash
scripts/test.sh                    # component tests: API, agent, proxy (~20 s; needs Docker)
scripts/test.sh e2e local          # end-to-end against the local stack (~2 min; scripts/local.sh up first)
scripts/test.sh e2e local --idle   # idle shutdown only, with the local API's timeout set to 1 minute (~1 min)
scripts/test.sh e2e gcp            # end-to-end against GCP (PROJECT_ID=... as for the other scripts)
```

The script creates `.venv` with the services' dependencies and pytest on first use. Anything
after the arguments above goes to pytest, e.g. `scripts/test.sh -k idle -x`.

## Component tests

Each part is tested on its own, locally. Real infrastructure is used where it matters
(PostgreSQL, Docker), with local stand-ins for Cloud Storage and Pub/Sub, so these tests need no
GCP project and create nothing on GCP.
Throwaway containers (PostgreSQL, fake-gcs-server) start on free ports and are removed at the end,
so a running local stack is left alone.

| Folder | Tests | Against |
|---|---|---|
| [test_api/](test_api/) | Sign-up and sign-in rules, sessions, ownership (another user's workspace is "not found" for every action); each action's state change and job; stale, duplicate and out-of-order agent reports; Pub/Sub being down; the structured log lines the metrics read; placement (spread, full workers, dead workers, previous worker, and no overfilling under concurrent requests); heartbeats; recovery from a dead worker; idle shutdown | FastAPI's test client, a real PostgreSQL, and a fake publisher that records each job |
| [test_agent/](test_agent/) | `start`, `stop` and `delete` each work and are safe to run twice; isolation settings (limits, no capabilities, non-root, own network); files survive stop and start through Cloud Storage; a failed start leaves nothing behind; containers the API moved elsewhere are stopped. How a message is handled: a failed job is reported or redelivered, never silently dropped; a stale job (the workspace moved to another worker meanwhile) is skipped | Real Docker, fake-gcs-server, and a small Alpine image with the same `coder` user as the real workspace image |
| [test_config/](test_config/) | The monitoring configuration: the dashboard only uses metrics `setup.sh` creates, its tiles fit without overlapping, and each log-based metric reads a log line and fields the API really writes | The files in `monitoring/` and `scripts/common.sh` |
| [test_proxy/](test_proxy/) | Links and cookies (expired, forged, another workspace's, a port's), port hosts and their sign-in, redirect rewriting, WebSockets, activity tracking, per-workspace log lines | The real proxy app in front of a fake code-server that echoes requests |

Time-based behaviour (dead workers, idle workspaces) is tested by backdating timestamps in the
database, so no test waits.

## End-to-end tests

[e2e/test_e2e.py](e2e/test_e2e.py) checks the proposal's success criteria on a running CloudSmith,
as a user: through the dashboard API and the workspace links, with an HTTP session as the browser.

1. Create a workspace, open the editor, check strangers and other users are kept out, and open an app on port 5000.
2. Write a file, stop the workspace, take its worker away, start it: it runs on the other worker with the file.
3. Archive a file, then lose the worker while the workspace runs: it comes back on the other worker with the file.
4. A worker dies just as a workspace is started on it; the workspace moves to the other worker. When the first worker returns, it skips the old start job instead of running a second copy.
5. (`--idle`) A workspace nobody opens is stopped automatically, with the note on the dashboard.

Writing and reading files, and starting the app, happen inside the workspace container
(`docker exec`, or `gcloud compute ssh` + `docker exec` on GCP), since there is no plain HTTP way
to type into the editor. Losing a worker is simulated by stopping its agent. Tests 2–4 need two
workers. Each test signs up a new `e2e-…` user and deletes its workspaces at the end; the accounts
stay, since the API cannot delete users (`scripts/local.sh clean` removes them locally).

Tests 2–4 take a worker away, so the API would move *any* workspace on it, restarting it from its
last archive. They are skipped when workspaces the tests didn't create are running, and `--idle`
refuses to start while any workspace runs (a 1-minute idle timeout would stop them too). Other
workspaces must be stopped first.

On GCP, `gcloud compute ssh` must be able to reach the worker VMs (port 22), and the idle test
is not run: it would need the API redeployed with a short timeout.
