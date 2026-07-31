# Job history database — production setup

VISTA-MCP writes **every job** to PostgreSQL: the tool call, every pipeline step, each analyzer's
live discovery document, the exact request that was sent to it, what it returned, ORB, and the
final report. The GUI's **Dashboard** and **History** views read from these tables, and the data
stays there permanently.

Everything here is **fail-open**: if `DATABASE_URL` is unset or the database is unreachable, the
server starts and serves tool calls exactly as it did before — only history and analytics are
unavailable (the GUI says so).

---

## 1. TL;DR

```bash
# 1. create the schema (once, as a user that can CREATE TABLE in the target database)
psql -h <host> -U <admin-user> -d usage_logs -f docs/db_setup.sql     # or paste §3 below

# 2. point the server at it
export DATABASE_URL="postgresql+psycopg://<user>:<password>@<host>:5432/usage_logs"
export MCP_DB_AUTO_CREATE=0        # schema is managed by hand in prod

# 3. restart VISTA-MCP and check the banner
#    job history (DB) : ON → postgresql://<user>:***@<host>:5432/usage_logs
```

---

## 2. Connection string

```
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/usage_logs
```

* The SQLAlchemy-style `+psycopg` suffix is accepted and stripped — plain
  `postgresql://…` works identically.
* Local development points at the prod **clone**:
  `postgresql+psycopg://jaskirat:jaskirat@localhost:5432/usage_logs`
* The password is never logged: the banner and the GUI show `postgresql://user:***@host/db`.
* Special characters in the password must be percent-encoded (`@` → `%40`, `/` → `%2F`).

---

## 3. Run these in prod `psql`

Four tables, all prefixed `mcp_` because `usage_logs` is shared with other VISTA services. Every
statement is `IF NOT EXISTS`, so re-running is safe.

```sql
-- ==========================================================================
-- VISTA-MCP job history — run once per environment, in the usage_logs database
-- ==========================================================================
\connect usage_logs

-- 1) one row per MCP tool call ---------------------------------------------
CREATE TABLE IF NOT EXISTS mcp_jobs (
    job_id            text PRIMARY KEY,
    tool_name         text        NOT NULL,
    question          text        NOT NULL DEFAULT '',
    filename          text        NOT NULL DEFAULT '',
    file_bytes        bigint      NOT NULL DEFAULT 0,
    status            text        NOT NULL DEFAULT 'running',
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    duration_ms       integer,
    mandatory_ids     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    optional_ids      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    discovered_ids    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    dropped_ids       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    selected_ids      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    used_ai           boolean     NOT NULL DEFAULT false,
    ai_mode           text        NOT NULL DEFAULT '',
    ai_reason         text        NOT NULL DEFAULT '',
    ai_system_prompt  text        NOT NULL DEFAULT '',
    ai_raw            text        NOT NULL DEFAULT '',
    ai_plan_source    text        NOT NULL DEFAULT '',
    ai_elapsed_ms     integer,
    orb_enabled       boolean     NOT NULL DEFAULT false,
    orb_status        text        NOT NULL DEFAULT '',
    orb_chars         integer     NOT NULL DEFAULT 0,
    orb_elapsed_ms    integer,
    report_chars      integer     NOT NULL DEFAULT 0,
    report_markdown   text        NOT NULL DEFAULT '',
    error             text,
    server_host       text        NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mcp_jobs_started_idx  ON mcp_jobs (started_at DESC);
CREATE INDEX IF NOT EXISTS mcp_jobs_tool_idx     ON mcp_jobs (tool_name, started_at DESC);
CREATE INDEX IF NOT EXISTS mcp_jobs_status_idx   ON mcp_jobs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS mcp_jobs_selected_idx ON mcp_jobs USING gin (selected_ids);

-- 2) one row per pipeline step event (the flow, in order) -------------------
CREATE TABLE IF NOT EXISTS mcp_job_events (
    id          bigserial PRIMARY KEY,
    job_id      text        NOT NULL REFERENCES mcp_jobs(job_id) ON DELETE CASCADE,
    seq         integer     NOT NULL,
    step        text        NOT NULL,
    status      text        NOT NULL DEFAULT '',
    detail      text        NOT NULL DEFAULT '',
    at          timestamptz NOT NULL DEFAULT now(),
    elapsed_ms  integer,
    data        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, seq)
);
CREATE INDEX IF NOT EXISTS mcp_job_events_job_idx ON mcp_job_events (job_id, seq);

-- 3) one row per analyzer per job (discovery + request sent + result) -------
CREATE TABLE IF NOT EXISTS mcp_job_analyzers (
    id                   bigserial PRIMARY KEY,
    job_id               text        NOT NULL REFERENCES mcp_jobs(job_id) ON DELETE CASCADE,
    analyzer_id          text        NOT NULL,
    title                text        NOT NULL DEFAULT '',
    api_url              text        NOT NULL DEFAULT '',
    mandatory            boolean     NOT NULL DEFAULT false,
    discovered           boolean     NOT NULL DEFAULT false,
    selected             boolean     NOT NULL DEFAULT false,
    called               boolean     NOT NULL DEFAULT false,
    ok                   boolean,
    http_status          integer,
    when_to_use          text        NOT NULL DEFAULT '',
    discovery            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    request_method       text        NOT NULL DEFAULT '',
    request_url          text        NOT NULL DEFAULT '',
    request_content_type text        NOT NULL DEFAULT '',
    request_file_param   text        NOT NULL DEFAULT '',
    request_fields       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    plan_source          text        NOT NULL DEFAULT '',
    plan_notes           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    elapsed_ms           integer,
    report_chars         integer     NOT NULL DEFAULT 0,
    report_markdown      text        NOT NULL DEFAULT '',
    meta                 jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error                text        NOT NULL DEFAULT '',
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, analyzer_id)
);
CREATE INDEX IF NOT EXISTS mcp_job_analyzers_aid_idx
    ON mcp_job_analyzers (analyzer_id, created_at DESC);

-- 4) audit trail of TOOL_ENABLEMENT saves from the GUI ----------------------
CREATE TABLE IF NOT EXISTS mcp_config_audit (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    action      text        NOT NULL DEFAULT 'save',
    actor       text        NOT NULL DEFAULT '',
    tools       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    config      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    note        text        NOT NULL DEFAULT ''
);
```

### If the app user is not the table owner

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_jobs, mcp_job_events,
      mcp_job_analyzers, mcp_config_audit TO <app-user>;
GRANT USAGE, SELECT ON SEQUENCE mcp_job_events_id_seq,
      mcp_job_analyzers_id_seq, mcp_config_audit_id_seq TO <app-user>;
```

The app never issues DDL when `MCP_DB_AUTO_CREATE=0`, so `CREATE` is not needed at runtime.

---

## 4. Verify

```sql
-- tables exist
\dt mcp_*

-- the most recent jobs
SELECT job_id, tool_name, status, duration_ms, selected_ids, used_ai, ai_plan_source,
       orb_status, report_chars, started_at
FROM mcp_jobs ORDER BY started_at DESC LIMIT 10;

-- one job's whole flow
SELECT seq, step, status, elapsed_ms, left(detail, 70)
FROM mcp_job_events WHERE job_id = '<job id>' ORDER BY seq;

-- what was actually sent to each analyzer on that job
SELECT analyzer_id, selected, called, ok, http_status, request_method, request_url,
       request_file_param, request_fields, plan_source, plan_notes, elapsed_ms
FROM mcp_job_analyzers WHERE job_id = '<job id>';

-- who changed the tool config, and to what
SELECT at, actor, tools FROM mcp_config_audit ORDER BY at DESC LIMIT 10;
```

From the server side, the startup banner and the GUI's left rail both report the connection, and
`GET /gui/api/state` returns a `db` block:

```json
"db": {"enabled": true, "connected": true, "url": "postgresql://user:***@host:5432/usage_logs",
       "queued": 0, "written": 42, "failed": 0, "dropped": 0, "store_reports": true}
```

---

## 5. Environment variables

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | _(unset)_ | Postgres DSN. Unset ⇒ history off, server otherwise unchanged |
| `MCP_DB_ENABLED` | `1` | `0` disables persistence even when `DATABASE_URL` is set |
| `MCP_DB_AUTO_CREATE` | `1` | `1` runs the `CREATE TABLE IF NOT EXISTS` above at startup. **Set `0` in prod** once the schema is created by hand |
| `MCP_DB_POOL_MAX` | `6` | max pooled connections |
| `MCP_DB_STORE_REPORTS` | `1` | `0` stores only lengths, not the report text (smaller DB, less drill-down) |
| `MCP_DB_QUEUE_MAX` | `20000` | in-process write-queue depth before records are dropped (never blocks a tool call) |
| `MCP_JOBS_MEMORY` | `60` | how many recent jobs the live GUI keeps in memory (history is in the DB) |

---

## 6. How writes behave

* Writes go to an **in-process queue drained by one background thread** — a tool call never waits
  for the database, and a slow or dead database costs it nothing.
* One writer means **ordering is guaranteed**: a job row always lands before its events.
* Each write retries once. Persistent failures are counted and logged (rate-limited), never
  raised into the request path.
* On shutdown the queue is drained (best effort, ~5 s) before the pool closes.

---

## 7. Growth and retention

Per job, roughly: the job row (a few KB plus the final report), one row per step (~15–20), and one
row per configured analyzer (including the discovery document, typically 1–4 KB each).
A busy day of a few thousand jobs is on the order of tens of MB.

Nothing is deleted automatically — jobs are meant to stay. If you ever need a retention policy,
deleting from `mcp_jobs` cascades to events and analyzer rows:

```sql
DELETE FROM mcp_jobs WHERE started_at < now() - interval '365 days';
```

To keep history but shed the bulk, blank the stored text instead:

```sql
UPDATE mcp_jobs SET report_markdown = '' WHERE started_at < now() - interval '90 days';
UPDATE mcp_job_analyzers a SET report_markdown = '', discovery = '{}'::jsonb
  FROM mcp_jobs j WHERE j.job_id = a.job_id AND j.started_at < now() - interval '90 days';
```

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Banner: `job history (DB) : OFF — no DATABASE_URL / disabled` | `DATABASE_URL` not set, or `MCP_DB_ENABLED=0` |
| Banner: `OFF — OperationalError: …` | Wrong host/credentials, or `pg_hba.conf` rejects the app user. The server still runs |
| `db: psycopg not installed` | `pip install 'psycopg[binary,pool]'` (it is in `pyproject.toml`) |
| Dashboard/History say "not connected" | Same as above — the GUI reflects `db.connected` |
| `db: write failed (… permission denied …)` | Grant the app user DML on the four tables (§3) |
| `db: write queue full — dropped N record(s)` | The database is far slower than the job rate; check DB health and `MCP_DB_POOL_MAX` |
