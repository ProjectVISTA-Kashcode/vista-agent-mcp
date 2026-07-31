"""DURABLE JOB HISTORY — every job, every step, every analyzer call, in PostgreSQL.

VISTA-MCP used to keep the last 30 jobs in memory and forget everything else. This module makes
the flow **permanent**: every tool call, every pipeline step, the AI Controller's prompt/answer/
plans, each analyzer's live discovery + the exact request that was sent + what came back, ORB,
and the final report — all written to Postgres and queryable forever from the GUI's Dashboard
and History views.

Four tables (all prefixed ``mcp_`` — the database is shared with other VISTA services):

    mcp_jobs           one row per tool call (the job + its outcome + the final report)
    mcp_job_events     one row per pipeline step event (the flow, in order)
    mcp_job_analyzers  one row per analyzer per job (discovery, request plan, result)
    mcp_config_audit   one row per TOOL_ENABLEMENT save (who changed what, when)

**Design rules**

* *Never block the pipeline.* Writes go onto an in-process queue drained by one background
  thread, so a slow or dead database costs a tool call nothing.
* *Never break a request.* Every failure is caught and logged; if Postgres is unreachable the
  server runs exactly as it did before — the live GUI still works off the in-memory registry,
  only history is lost.
* *One writer, in order.* A single FIFO thread means a job row always lands before its events.

Env:

    DATABASE_URL        postgresql+psycopg://user:pass@host:5432/usage_logs   (the ``+psycopg``
                        SQLAlchemy suffix is accepted and stripped)
    MCP_DB_ENABLED      1 (default when DATABASE_URL is set) — 0 disables persistence entirely
    MCP_DB_AUTO_CREATE  1 (default) — create the tables if missing (idempotent). Set 0 in
                        production if the schema is managed by hand (see docs/db_setup.md)
    MCP_DB_POOL_MAX     max pooled connections (default 6)
    MCP_DB_STORE_REPORTS 1 (default) — store the full report text. 0 stores only lengths.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import vlog

from .models import AnalyzerRun, Job, JobEvent

try:  # psycopg3 + its pool. Absent ⇒ persistence is simply off.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
    from psycopg_pool import ConnectionPool
    _HAVE_PSYCOPG = True
except Exception:  # noqa: BLE001
    _HAVE_PSYCOPG = False
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore
    Json = None  # type: ignore
    ConnectionPool = None  # type: ignore


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ENABLED = (
    os.getenv("MCP_DB_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    and bool(DATABASE_URL)
)
AUTO_CREATE = os.getenv("MCP_DB_AUTO_CREATE", "1").strip().lower() not in ("0", "false", "no", "off")
POOL_MAX = int(os.getenv("MCP_DB_POOL_MAX", "6"))
STORE_REPORTS = os.getenv("MCP_DB_STORE_REPORTS", "1").strip().lower() not in ("0", "false", "no", "off")
QUEUE_MAX = int(os.getenv("MCP_DB_QUEUE_MAX", "20000"))

_STATE: dict[str, Any] = {
    "connected": False, "written": 0, "failed": 0, "dropped": 0,
    "last_error": "", "started": False,
}
_POOL = None
_Q: "queue.Queue[tuple[str, tuple] | None]" = queue.Queue(maxsize=QUEUE_MAX)
_WRITER: threading.Thread | None = None
_LOCK = threading.Lock()


def conninfo() -> str:
    """The libpq connection string — accepts the SQLAlchemy ``postgresql+psycopg://`` form."""
    url = DATABASE_URL
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def safe_url() -> str:
    """The connection target with the password removed (safe for logs and the GUI)."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", conninfo())


# --------------------------------------------------------------------------- #
# Schema — kept in one place so docs/db_setup.md and the auto-create path agree
# --------------------------------------------------------------------------- #
DDL: list[str] = [
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS mcp_jobs_started_idx   ON mcp_jobs (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS mcp_jobs_tool_idx      ON mcp_jobs (tool_name, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS mcp_jobs_status_idx    ON mcp_jobs (status, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS mcp_jobs_selected_idx  ON mcp_jobs USING gin (selected_ids)",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS mcp_job_events_job_idx ON mcp_job_events (job_id, seq)",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS mcp_job_analyzers_aid_idx ON mcp_job_analyzers (analyzer_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS mcp_config_audit (
        id          bigserial PRIMARY KEY,
        at          timestamptz NOT NULL DEFAULT now(),
        action      text        NOT NULL DEFAULT 'save',
        actor       text        NOT NULL DEFAULT '',
        tools       jsonb       NOT NULL DEFAULT '[]'::jsonb,
        config      jsonb       NOT NULL DEFAULT '{}'::jsonb,
        note        text        NOT NULL DEFAULT ''
    )
    """,
]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def _ts(epoch: float | None):
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else None


def init() -> bool:
    """Open the pool, create the schema if allowed, start the writer. Never raises."""
    global _POOL, _WRITER
    if not ENABLED:
        vlog.log("db: persistence DISABLED (no DATABASE_URL or MCP_DB_ENABLED=0) — "
                 "jobs stay in memory only", vlog.WARNING)
        return False
    if not _HAVE_PSYCOPG:
        _STATE["last_error"] = "psycopg is not installed"
        vlog.log("db: psycopg not installed — job history disabled "
                 "(pip install 'psycopg[binary,pool]')", vlog.WARNING)
        return False
    with _LOCK:
        if _STATE["started"]:
            return _STATE["connected"]
        _STATE["started"] = True
        try:
            _POOL = ConnectionPool(
                conninfo(), min_size=1, max_size=POOL_MAX, open=False, timeout=10,
                kwargs={"autocommit": True, "connect_timeout": 10},
                name="vista-mcp",
            )
            _POOL.open(wait=True, timeout=15)
            if AUTO_CREATE:
                with _POOL.connection() as conn, conn.cursor() as cur:
                    for stmt in DDL:
                        cur.execute(stmt)
                vlog.log("db: schema verified / created (mcp_jobs, mcp_job_events, "
                         "mcp_job_analyzers, mcp_config_audit)")
            _STATE["connected"] = True
            vlog.log(f"db: connected → {safe_url()}  (pool max {POOL_MAX}, "
                     f"reports {'stored' if STORE_REPORTS else 'not stored'})")
        except Exception as e:  # noqa: BLE001
            _STATE["connected"] = False
            _STATE["last_error"] = f"{type(e).__name__}: {e}"
            vlog.log(f"db: could NOT connect ({type(e).__name__}: {e}) — history disabled, "
                     f"the server runs normally", vlog.WARNING)
            return False

        _WRITER = threading.Thread(target=_writer_loop, name="vista-db-writer", daemon=True)
        _WRITER.start()
        return True


def shutdown(timeout: float = 5.0) -> None:
    """Drain the write queue and close the pool (best effort, on server exit)."""
    if not _STATE["connected"]:
        return
    try:
        _Q.put_nowait(None)
        if _WRITER is not None:
            _WRITER.join(timeout=timeout)
    except Exception:  # noqa: BLE001
        pass
    try:
        if _POOL is not None:
            _POOL.close()
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    return {
        "enabled": ENABLED,
        "connected": bool(_STATE["connected"]),
        "url": safe_url() if ENABLED else "",
        "queued": _Q.qsize(),
        "written": _STATE["written"],
        "failed": _STATE["failed"],
        "dropped": _STATE["dropped"],
        "store_reports": STORE_REPORTS,
        "last_error": _STATE["last_error"],
    }


# --------------------------------------------------------------------------- #
# The single background writer
# --------------------------------------------------------------------------- #
def _enqueue(op: str, args: tuple) -> None:
    if not _STATE["connected"]:
        return
    try:
        _Q.put_nowait((op, args))
    except queue.Full:
        _STATE["dropped"] += 1
        if _STATE["dropped"] % 100 == 1:
            vlog.log(f"db: write queue full — dropped {_STATE['dropped']} record(s)", vlog.WARNING)


def _writer_loop() -> None:
    while True:
        item = _Q.get()
        if item is None:
            break
        op, args = item
        for attempt in (1, 2):
            try:
                _EXECUTORS[op](*args)
                _STATE["written"] += 1
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    time.sleep(0.25)
                    continue
                _STATE["failed"] += 1
                _STATE["last_error"] = f"{op}: {type(e).__name__}: {e}"
                if _STATE["failed"] % 50 == 1:
                    vlog.log(f"db: write failed ({op}: {type(e).__name__}: {e})", vlog.WARNING)


def _exec(sql: str, params: tuple) -> None:
    with _POOL.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


# ---- write ops ------------------------------------------------------------- #
_INSERT_JOB = """
INSERT INTO mcp_jobs (job_id, tool_name, question, filename, file_bytes, status,
                      started_at, server_host)
VALUES (%s,%s,%s,%s,%s,'running',%s,%s)
ON CONFLICT (job_id) DO NOTHING
"""


def _w_job_start(job_id, tool_name, question, filename, file_bytes, started_at, host) -> None:
    _exec(_INSERT_JOB, (job_id, tool_name, question, filename, file_bytes,
                        _ts(started_at), host))


_INSERT_EVENT = """
INSERT INTO mcp_job_events (job_id, seq, step, status, detail, at, elapsed_ms, data)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (job_id, seq) DO NOTHING
"""


def _w_event(job_id, seq, step, status_, detail, at, elapsed_ms, data) -> None:
    _exec(_INSERT_EVENT, (job_id, seq, step, status_, detail, _ts(at), elapsed_ms, Json(data)))


_UPSERT_ANALYZER = """
INSERT INTO mcp_job_analyzers (
    job_id, analyzer_id, title, api_url, mandatory, discovered, selected, called, ok,
    http_status, when_to_use, discovery, request_method, request_url, request_content_type,
    request_file_param, request_fields, plan_source, plan_notes, elapsed_ms, report_chars,
    report_markdown, meta, error)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (job_id, analyzer_id) DO UPDATE SET
    title=EXCLUDED.title, api_url=EXCLUDED.api_url, mandatory=EXCLUDED.mandatory,
    discovered=EXCLUDED.discovered, selected=EXCLUDED.selected, called=EXCLUDED.called,
    ok=EXCLUDED.ok, http_status=EXCLUDED.http_status, when_to_use=EXCLUDED.when_to_use,
    discovery=EXCLUDED.discovery, request_method=EXCLUDED.request_method,
    request_url=EXCLUDED.request_url, request_content_type=EXCLUDED.request_content_type,
    request_file_param=EXCLUDED.request_file_param, request_fields=EXCLUDED.request_fields,
    plan_source=EXCLUDED.plan_source, plan_notes=EXCLUDED.plan_notes,
    elapsed_ms=EXCLUDED.elapsed_ms, report_chars=EXCLUDED.report_chars,
    report_markdown=EXCLUDED.report_markdown, meta=EXCLUDED.meta, error=EXCLUDED.error
"""


def _w_analyzer(job_id, r: dict, http_status) -> None:
    plan = r.get("plan") or {}
    _exec(_UPSERT_ANALYZER, (
        job_id, r["analyzer_id"], r.get("title", ""), r.get("api_url", ""),
        r.get("mandatory", False), r.get("discovered", False), r.get("selected", False),
        r.get("called", False), r.get("ok"), http_status, r.get("when_to_use", ""),
        Json(r.get("discovery") or {}), plan.get("method", ""), plan.get("url", ""),
        plan.get("content_type", ""), plan.get("file_param", ""), Json(plan.get("fields") or {}),
        plan.get("source", ""), Json(plan.get("notes") or []), r.get("elapsed_ms"),
        r.get("report_chars", 0), (r.get("report_markdown", "") if STORE_REPORTS else ""),
        Json(r.get("meta") or {}), r.get("error", ""),
    ))


_UPDATE_JOB = """
UPDATE mcp_jobs SET
    status=%s, finished_at=%s, duration_ms=%s,
    mandatory_ids=%s, optional_ids=%s, discovered_ids=%s, dropped_ids=%s, selected_ids=%s,
    used_ai=%s, ai_mode=%s, ai_reason=%s, ai_system_prompt=%s, ai_raw=%s, ai_plan_source=%s,
    ai_elapsed_ms=%s, orb_enabled=%s, orb_status=%s, orb_chars=%s, orb_elapsed_ms=%s,
    report_chars=%s, report_markdown=%s, error=%s, question=%s, filename=%s, file_bytes=%s,
    updated_at=now()
WHERE job_id=%s
"""


def _w_job_finish(j: dict) -> None:
    _exec(_UPDATE_JOB, (
        j["status"], _ts(j.get("finished_at")), j.get("duration_ms"),
        Json(j.get("mandatory_ids") or []), Json(j.get("optional_ids") or []),
        Json(j.get("discovered_ids") or []), Json(j.get("dropped_ids") or []),
        Json(j.get("selected_ids") or []),
        j.get("used_ai", False), j.get("ai_mode", ""), j.get("ai_reason", ""),
        j.get("ai_system_prompt", ""), j.get("ai_raw", ""), j.get("ai_plan_source", ""),
        j.get("ai_elapsed_ms"), j.get("orb_enabled", False), j.get("orb_status", ""),
        j.get("orb_chars", 0), j.get("orb_elapsed_ms"), j.get("report_chars", 0),
        (j.get("report_markdown", "") if STORE_REPORTS else ""), j.get("error"),
        j.get("question", ""), j.get("filename", ""), j.get("file_bytes", 0),
        j["job_id"],
    ))


_INSERT_AUDIT = """
INSERT INTO mcp_config_audit (action, actor, tools, config, note)
VALUES (%s,%s,%s,%s,%s)
"""


def _w_audit(action, actor, tools, config, note) -> None:
    _exec(_INSERT_AUDIT, (action, actor, Json(tools), Json(config), note))


_EXECUTORS = {
    "job_start": _w_job_start,
    "event": _w_event,
    "analyzer": _w_analyzer,
    "job_finish": _w_job_finish,
    "audit": _w_audit,
}


# ---- public write API (called from the pipeline; all fire-and-forget) ------- #
_HOST = os.getenv("HOSTNAME", "") or os.uname().nodename


def record_job_start(job: Job) -> None:
    _enqueue("job_start", (job.job_id, job.tool_name, job.question, job.filename,
                           job.file_bytes, job.started_at, _HOST))


def record_event(job_id: str, seq: int, ev: JobEvent) -> None:
    _enqueue("event", (job_id, seq, ev.step, ev.status, ev.detail, ev.at, ev.elapsed_ms,
                       _jsonable(ev.data)))


def record_analyzer(job_id: str, run: AnalyzerRun, http_status: int | None = None) -> None:
    _enqueue("analyzer", (job_id, run.model_dump(mode="json"), http_status))


def record_job_finish(job: Job) -> None:
    d = job.model_dump(mode="json", exclude={"events", "analyzers"})
    d["duration_ms"] = (int((job.finished_at - job.started_at) * 1000)
                        if job.finished_at else None)
    d["selected_ids"] = job.selected_analyzers
    _enqueue("job_finish", (d,))


def record_config_save(tools: list[str], config: dict, actor: str = "gui",
                       action: str = "save", note: str = "") -> None:
    _enqueue("audit", (action, actor, tools, config, note))


def _jsonable(value: Any) -> Any:
    """Make a step's ``data`` dict safe for jsonb (bytes/objects → strings)."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# Reads — used by the GUI (Dashboard + History). Run these off the event loop.
# --------------------------------------------------------------------------- #
def _num(v):
    """Postgres numerics arrive as Decimal — make them JSON-safe."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return 0
    return int(f) if f.is_integer() else round(f, 2)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _POOL.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _one(sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _where(f: dict) -> tuple[str, list]:
    """Build the shared WHERE clause for history + analytics filters."""
    clauses: list[str] = []
    params: list = []
    if f.get("tool"):
        clauses.append("j.tool_name = %s")
        params.append(f["tool"])
    if f.get("status"):
        clauses.append("j.status = %s")
        params.append(f["status"])
    if f.get("analyzer"):
        clauses.append("j.selected_ids @> %s::jsonb")
        params.append(f'["{f["analyzer"]}"]')
    if f.get("orb") in ("on", "off"):
        clauses.append("j.orb_status = 'ok'" if f["orb"] == "on" else "j.orb_status <> 'ok'")
    if f.get("ai") in ("on", "off"):
        clauses.append("j.used_ai = %s")
        params.append(f["ai"] == "on")
    if f.get("since_hours"):
        clauses.append("j.started_at >= now() - (%s || ' hours')::interval")
        params.append(str(int(f["since_hours"])))
    if f.get("from"):
        clauses.append("j.started_at >= %s")
        params.append(f["from"])
    if f.get("to"):
        clauses.append("j.started_at <= %s")
        params.append(f["to"])
    if f.get("q"):
        clauses.append("(j.question ILIKE %s OR j.filename ILIKE %s OR j.job_id ILIKE %s "
                       "OR j.tool_name ILIKE %s)")
        like = f"%{f['q']}%"
        params += [like, like, like, like]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_jobs(filters: dict, limit: int = 50, offset: int = 0) -> dict:
    """Filtered, paginated job history (newest first) + the matching total."""
    if not _STATE["connected"]:
        return {"jobs": [], "total": 0, "available": False}
    where, params = _where(filters)
    total = (_one(f"SELECT count(*) AS n FROM mcp_jobs j{where}", tuple(params)) or {}).get("n", 0)
    rows = _rows(
        f"""SELECT j.job_id, j.tool_name, j.question, j.filename, j.file_bytes, j.status,
                   j.started_at, j.finished_at, j.duration_ms, j.selected_ids, j.used_ai,
                   j.ai_reason, j.ai_plan_source, j.orb_enabled, j.orb_status, j.report_chars,
                   j.error
            FROM mcp_jobs j{where}
            ORDER BY j.started_at DESC LIMIT %s OFFSET %s""",
        tuple(params + [limit, offset]),
    )
    return {"jobs": [_job_row(r) for r in rows], "total": total, "available": True}


def _job_row(r: dict) -> dict:
    return {
        "job_id": r["job_id"], "tool_name": r["tool_name"], "question": r["question"],
        "filename": r["filename"], "file_bytes": r.get("file_bytes", 0), "status": r["status"],
        "started_at": r["started_at"].timestamp() if r.get("started_at") else None,
        "finished_at": r["finished_at"].timestamp() if r.get("finished_at") else None,
        "duration_s": round(r["duration_ms"] / 1000, 1) if r.get("duration_ms") else None,
        "selected_analyzers": r.get("selected_ids") or [],
        "used_ai": r.get("used_ai", False), "ai_reason": r.get("ai_reason", ""),
        "ai_plan_source": r.get("ai_plan_source", ""),
        "orb_enabled": r.get("orb_enabled", False), "orb_status": r.get("orb_status", ""),
        "report_chars": r.get("report_chars", 0), "error": r.get("error"),
        "persisted": True,
    }


def get_job(job_id: str) -> dict | None:
    """One job's full record: summary + ordered events + per-analyzer detail + report."""
    if not _STATE["connected"]:
        return None
    j = _one("SELECT * FROM mcp_jobs WHERE job_id=%s", (job_id,))
    if not j:
        return None
    events = _rows("SELECT seq, step, status, detail, at, elapsed_ms, data FROM mcp_job_events "
                   "WHERE job_id=%s ORDER BY seq", (job_id,))
    analyzers = _rows("SELECT * FROM mcp_job_analyzers WHERE job_id=%s ORDER BY id", (job_id,))
    summary = _job_row(j)
    summary.update({
        "ai_system_prompt": j.get("ai_system_prompt", ""), "ai_raw": j.get("ai_raw", ""),
        "ai_mode": j.get("ai_mode", ""), "ai_elapsed_ms": j.get("ai_elapsed_ms"),
        "mandatory_ids": j.get("mandatory_ids") or [], "optional_ids": j.get("optional_ids") or [],
        "discovered_ids": j.get("discovered_ids") or [], "dropped_ids": j.get("dropped_ids") or [],
        "orb_chars": j.get("orb_chars", 0), "orb_elapsed_ms": j.get("orb_elapsed_ms"),
        "server_host": j.get("server_host", ""),
    })
    return {
        "summary": summary,
        "report_markdown": j.get("report_markdown", ""),
        "events": [{
            "step": e["step"], "status": e["status"], "detail": e["detail"],
            "at": e["at"].timestamp() if e.get("at") else None,
            "elapsed_ms": e.get("elapsed_ms"), "data": e.get("data") or {},
        } for e in events],
        "analyzers": [{
            "analyzer_id": a["analyzer_id"], "title": a["title"], "api_url": a["api_url"],
            "mandatory": a["mandatory"], "discovered": a["discovered"], "selected": a["selected"],
            "called": a["called"], "ok": a["ok"], "http_status": a.get("http_status"),
            "when_to_use": a.get("when_to_use", ""), "discovery": a.get("discovery") or {},
            "plan": {
                "method": a.get("request_method", ""), "url": a.get("request_url", ""),
                "content_type": a.get("request_content_type", ""),
                "file_param": a.get("request_file_param", ""),
                "fields": a.get("request_fields") or {},
                "source": a.get("plan_source", ""), "notes": a.get("plan_notes") or [],
            },
            "elapsed_ms": a.get("elapsed_ms"), "report_chars": a.get("report_chars", 0),
            "report_markdown": a.get("report_markdown", ""), "meta": a.get("meta") or {},
            "error": a.get("error", ""),
        } for a in analyzers],
    }


def analytics(filters: dict) -> dict:
    """Everything the Dashboard shows, in five aggregate queries."""
    if not _STATE["connected"]:
        return {"available": False}
    where, params = _where(filters)
    p = tuple(params)

    totals = _one(f"""
        SELECT count(*)                                            AS total,
               count(*) FILTER (WHERE j.status='done')             AS done,
               count(*) FILTER (WHERE j.status='error')            AS errored,
               count(*) FILTER (WHERE j.status='running')          AS running,
               count(*) FILTER (WHERE j.used_ai)                   AS ai_jobs,
               count(*) FILTER (WHERE j.ai_plan_source='ai')       AS ai_plans,
               count(*) FILTER (WHERE j.orb_status='ok')           AS orb_jobs,
               coalesce(avg(j.duration_ms),0)                      AS avg_ms,
               coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY j.duration_ms),0)  AS p50_ms,
               coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY j.duration_ms),0) AS p95_ms,
               coalesce(max(j.duration_ms),0)                      AS max_ms,
               coalesce(sum(j.report_chars),0)                     AS report_chars,
               coalesce(sum(j.file_bytes),0)                       AS file_bytes
        FROM mcp_jobs j{where}""", p) or {}

    bucket = filters.get("bucket") or "hour"
    if bucket not in ("hour", "day", "week", "minute"):
        bucket = "hour"
    series = _rows(f"""
        SELECT date_trunc('{bucket}', j.started_at) AS t,
               count(*) AS n,
               count(*) FILTER (WHERE j.status='error') AS errors,
               coalesce(avg(j.duration_ms),0) AS avg_ms
        FROM mcp_jobs j{where}
        GROUP BY 1 ORDER BY 1""", p)

    by_tool = _rows(f"""
        SELECT j.tool_name AS name, count(*) AS n,
               count(*) FILTER (WHERE j.status='error') AS errors,
               coalesce(avg(j.duration_ms),0) AS avg_ms
        FROM mcp_jobs j{where}
        GROUP BY 1 ORDER BY n DESC LIMIT 20""", p)

    by_analyzer = _rows(f"""
        SELECT a.analyzer_id AS name,
               count(*) FILTER (WHERE a.selected)              AS selected,
               count(*) FILTER (WHERE a.called)                AS called,
               count(*) FILTER (WHERE a.ok)                    AS ok,
               count(*) FILTER (WHERE a.called AND NOT coalesce(a.ok,false)) AS failed,
               coalesce(avg(a.elapsed_ms) FILTER (WHERE a.called),0) AS avg_ms,
               count(*) FILTER (WHERE a.plan_source='ai')      AS ai_plans,
               count(*) FILTER (WHERE a.plan_source='ai-corrected') AS corrected_plans
        FROM mcp_job_analyzers a JOIN mcp_jobs j ON j.job_id=a.job_id{where}
        GROUP BY 1 ORDER BY selected DESC LIMIT 30""", p)

    errors = _rows(f"""
        SELECT j.job_id, j.tool_name, j.question, j.error,
               j.started_at, j.duration_ms
        FROM mcp_jobs j{where}{' AND' if where else ' WHERE'} j.status='error'
        ORDER BY j.started_at DESC LIMIT 12""", p)

    return {
        "available": True,
        "totals": {k: _num(v) for k, v in totals.items()},
        "series": [{"t": r["t"].isoformat(), "n": _num(r["n"]), "errors": _num(r["errors"]),
                    "avg_ms": _num(r["avg_ms"])} for r in series],
        "by_tool": [{"name": r["name"], "n": _num(r["n"]), "errors": _num(r["errors"]),
                     "avg_ms": _num(r["avg_ms"])} for r in by_tool],
        "by_analyzer": [{"name": r["name"], "selected": _num(r["selected"]),
                         "called": _num(r["called"]), "ok": _num(r["ok"]),
                         "failed": _num(r["failed"]), "avg_ms": _num(r["avg_ms"]),
                         "ai_plans": _num(r["ai_plans"]),
                         "corrected_plans": _num(r["corrected_plans"])}
                        for r in by_analyzer],
        "errors": [{"job_id": r["job_id"], "tool_name": r["tool_name"],
                    "question": r["question"], "error": r["error"],
                    "started_at": r["started_at"].timestamp() if r.get("started_at") else None,
                    "duration_s": round(r["duration_ms"] / 1000, 1) if r.get("duration_ms") else None}
                   for r in errors],
        "bucket": bucket,
    }


def facets() -> dict:
    """Distinct values the History filters offer (tools and analyzers actually seen)."""
    if not _STATE["connected"]:
        return {"tools": [], "analyzers": [], "available": False}
    tools = [r["tool_name"] for r in _rows(
        "SELECT DISTINCT tool_name FROM mcp_jobs ORDER BY 1 LIMIT 100")]
    analyzers = [r["analyzer_id"] for r in _rows(
        "SELECT DISTINCT analyzer_id FROM mcp_job_analyzers ORDER BY 1 LIMIT 100")]
    return {"tools": tools, "analyzers": analyzers, "available": True}
