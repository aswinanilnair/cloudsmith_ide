# CloudSmith: Cloud-Hosted On Demand IDE

CloudSmith gives each user their own VS Code workspace in the browser, with a terminal and Python,
on demand. Users sign up, then create, open, stop, start and delete workspaces from a web
dashboard. Each workspace is an isolated [code-server](https://github.com/coder/code-server)
container on a pool of worker VMs on GCP. Stopped workspaces are saved to Cloud Storage and can
start again on any worker. Workspaces left unused are stopped automatically, and workspaces on a
failed worker are restarted on another one.

Final project for CSCI 5253: Datacenter Scale Computing, University of Colorado Boulder.

## Team

* Aswin Anil Nair (aswin.nair@colorado.edu)

## Live application

**Link to Application:** TBD

Workspaces open on the worker VMs at `http://ws-<id>.<worker-ip>.sslip.io/`. The dashboard links
to them, so there is nothing else to configure.

## Architecture

![CloudSmith architecture: the browser talks to the API on Cloud Run, which stores state in Cloud SQL and publishes jobs to Pub/Sub; an agent on each worker VM runs the jobs with Docker and archives workspace files to Cloud Storage, and a proxy on each worker routes the browser to its workspace](docs/architecture.png)

The numbers follow one request: the dashboard asks the API to start a workspace (1), the API
saves the new state (2) and publishes a job (3), the worker's agent pulls it (4), starts the
container (5) and reports back (6), the dashboard shows the new status (7), and the user opens the
workspace through the worker's proxy (8). Workspace files are archived to Cloud Storage when a
workspace stops and restored when it starts, on any worker.

| Part | Where | Role |
|---|---|---|
| API + dashboard | [services/api](services/api) | Accounts, workspace actions, placing workspaces on workers, recovery, idle shutdown |
| Agent | [services/agent](services/agent) | Runs on each worker; starts, stops and deletes workspace containers |
| Proxy | [services/proxy](services/proxy) | Runs on each worker; routes browsers to the right workspace and checks access |
| Workspace image | [images/workspace](images/workspace) | code-server with Python 3 |
| Monitoring | [monitoring](monitoring) | Cloud Monitoring dashboard and log-based metrics |
| Scripts | [scripts](scripts) | Local run, GCP setup, deploy, teardown, tests |
| Tests | [tests](tests) | Component and end-to-end tests ([tests/README.md](tests/README.md)) |

**Workspace states:**

```
create ──► PENDING ──► RUNNING ──► STOPPING ──► STOPPED
            ▲   │                                  │
            │   └──► FAILED                        │
            │          │                           │
            └──────────┴───────── start ───────────┘
```

* **PENDING → RUNNING:** the worker has started the workspace container.
* **PENDING → FAILED:** the start failed; the error is shown on the dashboard.
* **RUNNING → STOPPING:** the user stopped it, or nobody used it for 30 minutes.
* **STOPPING → STOPPED:** its files are archived to Cloud Storage.
* **STOPPED or FAILED → PENDING:** started again, on any worker.
* **Worker lost:** its workspaces go back to `PENDING` on another worker, or to `STOPPED` if no
  worker has room.

> [!NOTE]
> A running workspace is archived only when it stops. If its worker is lost while it runs, it is
> restored from its last archive, so changes made since it last started are lost (all of them, for
> a workspace that has never been stopped). The lost worker's copy is not deleted: when that worker
> comes back, the copy is only stopped and stays on its disk. If the workspace later moves back to
> that worker before any newer archive exists, the old copy is reused and its files reappear, while
> anything written on the other worker stays behind there. Copies left behind this way are not
> cleaned up automatically.
>
> Periodic snapshots of running workspaces would limit this to the last few minutes, but are not
> implemented yet because of their cost in worker CPU and upload traffic. They are future scope for
> the project.

## Setup and deploy on GCP

**Prerequisites:** a GCP project with billing enabled (Owner role), the
[`gcloud` CLI](https://cloud.google.com/sdk/docs/install), Docker with `buildx`, `bash` and
`openssl`. The worker VMs and Cloud SQL are billed while they exist.

**Settings.** All of these can be set before running the scripts, by exporting them as
environment variables. Only `PROJECT_ID` is required; the rest have
defaults in [scripts/common.sh](scripts/common.sh). Export the same values
for every script.

| Variable | Default | What it sets |
|---|---|---|
| `PROJECT_ID` | the `gcloud` default project | **The GCP project to deploy into. Set this explicitly.** |
| `REGION` / `ZONE` | `us-central1` / `us-central1-a` | Where everything runs |
| `WORKER_COUNT` | `2` | Number of worker VMs |
| `WORKER_MACHINE_TYPE` | `e2-standard-2` | Worker VM size (2 vCPUs, 8 GB) |
| `WORKER_DISK_GB` | `50` | Worker VM disk size |
| `WORKER_CAPACITY` | `4` | Workspaces each worker may run at once |
| `WS_CPU_LIMIT` / `WS_MEM_LIMIT_MB` | `1.0` / `1024` | CPU and memory limit per workspace |
| `MAX_WORKSPACES_PER_USER` | `3` | Workspaces each user may have |
| `IDLE_TIMEOUT_MINUTES` | `30` | Stop a workspace after this long unused; `0` turns it off |
| `ALLOW_SIGNUP` | `true` | Whether new users can sign up |
| `BUCKET` | `<project>-cloudsmith-workspaces` | Bucket for workspace archives (must be globally unique) |

**Commands:**

```bash
git clone <REPO>
cd <repo-folder>     # run every script from the top of the repo

gcloud auth login
export PROJECT_ID=<project-id>
# optional: any setting from the table, for example
# export WORKER_COUNT=3 WORKER_CAPACITY=4 IDLE_TIMEOUT_MINUTES=15

scripts/setup.sh     # once: creates all GCP resources (~15 min)
scripts/deploy.sh    # builds and deploys everything; prints the application URL (~5-10 min)
```

Open the printed URL, sign up and create a workspace. Re-run `scripts/deploy.sh` after any code
or setting change. Secrets are generated by `setup.sh` into `.env.gcp`; keep that file and never
commit it.

## Teardown

```bash
export PROJECT_ID=<project-id>   # plus REGION and BUCKET if they were changed
scripts/teardown.sh   # deletes every CloudSmith resource, including all accounts and workspaces (asks first)
```

GCP may keep the Cloud SQL instance name reserved for up to a week after deletion, so setting up
again in the same project soon afterwards can fail at the Cloud SQL step.

## Run locally

Needs Docker Desktop (4 GB of memory or more) and Chrome or Firefox. Safari does not work, because
it does not resolve `*.localhost`.

```bash
scripts/local.sh up      # build and start everything; dashboard at http://localhost:8000
scripts/local.sh logs    # follow the logs
scripts/local.sh down    # stop (accounts and workspaces are kept)
scripts/local.sh clean   # stop and delete all data
```

> [!NOTE]
> Locally, Docker Compose stands in for GCP: PostgreSQL for Cloud SQL, the Pub/Sub emulator,
> fake-gcs-server for Cloud Storage, and two workers on ports 8080 and 8081.

## Tests

```bash
scripts/test.sh             # component tests for the API, agent and proxy (needs Docker)
scripts/test.sh e2e local   # end-to-end, against scripts/local.sh up
scripts/test.sh e2e gcp     # end-to-end, against the GCP deployment
```

See [tests/README.md](tests/README.md) for what each test covers.
