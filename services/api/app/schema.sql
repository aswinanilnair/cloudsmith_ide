-- CloudSmith schema. Idempotent: the API runs it on every start.

CREATE TABLE IF NOT EXISTS users (
  id             UUID PRIMARY KEY,
  username       TEXT UNIQUE NOT NULL,
  password_hash  TEXT NOT NULL,            -- <salt>$<pbkdf2-sha256 hex>
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per worker VM, created and kept fresh by the agent's heartbeats.
CREATE TABLE IF NOT EXISTS workers (
  id              TEXT PRIMARY KEY,         -- the agent's WORKER_ID, e.g. worker-1
  workspace_url   TEXT NOT NULL,            -- where its workspaces open, e.g. http://ws-{id}.<ip>.sslip.io/
  capacity        INT NOT NULL,             -- how many workspaces it may run at once
  state           TEXT NOT NULL,            -- UP | DOWN (missed heartbeats)
  last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspaces (
  id          UUID PRIMARY KEY,
  owner_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  state       TEXT NOT NULL,               -- PENDING | RUNNING | STOPPING | STOPPED | FAILED
  error       TEXT,                        -- why the last job failed, or a note such as an idle stop
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The worker that holds the workspace's container. NULL once it is stopped: its files then
-- live only in Cloud Storage and it can start on any worker.
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS worker_id TEXT REFERENCES workers(id);
-- The last time a browser used the workspace, as reported by the proxy (for idle shutdown).
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ;

-- When it last became RUNNING (the dashboard shows its uptime). NULL once it stops.
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
-- Workspaces already running when the column was added: their state last changed when they
-- became RUNNING (nothing else updates updated_at while a workspace runs).
UPDATE workspaces SET started_at = updated_at WHERE state = 'RUNNING' AND started_at IS NULL;
-- The worker's VM name as its agent reports it, e.g. cloudsmith-worker-1 (shown on the dashboard).
ALTER TABLE workers ADD COLUMN IF NOT EXISTS vm_name TEXT;

CREATE INDEX IF NOT EXISTS workspaces_owner_idx ON workspaces (owner_id);
CREATE INDEX IF NOT EXISTS workspaces_worker_idx ON workspaces (worker_id);
