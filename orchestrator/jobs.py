"""Job + flow tracking — the source of truth for the CLI logs, the flow GUI, and the database.

Every tool invocation is a :class:`~orchestrator.models.Job`. Each pipeline step appends a
:class:`~orchestrator.models.JobEvent` (intake → vfr → tool_enablement → discover →
ai_controller → analyze:<id> … → concat → orb → done). ``emit()`` does three things at once:

  * writes a structured line to the terminal via ``vlog`` (extensive CLI logging),
  * stores the event on the in-memory job so the GUI can render the live flow, and
  * queues the event for the database so the job is still there tomorrow.

The in-memory registry is a **live window** (the most recent N jobs, for the running flow view);
:mod:`orchestrator.db` is the permanent record the Dashboard and History views read. If the
database is unreachable everything here still works — only history is lost.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

import vlog

from . import db
from .models import AnalyzerRun, Job, JobEvent

# The live window: enough to watch concurrent runs animate. Durable history lives in Postgres,
# so this can stay small and cheap.
_MAX_JOBS = int(os.getenv("MCP_JOBS_MEMORY", "60"))
_JOBS: "OrderedDict[str, Job]" = OrderedDict()
_LOCK = threading.Lock()

# per-step display metadata for the GUI (icon + human label)
STEP_META = {
    "intake": ("📥", "Tool Call & Fetch"),
    "vfr": ("🧭", "VFR (routing)"),
    "tool_enablement": ("⚙️", "Tool Enablement"),
    "discover": ("🔎", "Discover Tools"),
    "ai_controller": ("🧠", "AI Controller"),
    "analyze": ("📊", "Analyzer"),
    "orb": ("🛟", "ORB Suggestions"),
    "concat": ("🧩", "Concatenate"),
    "done": ("✅", "Answer & Back"),
}


def start(job_id: str, tool_name: str, question: str, filename: str,
          file_bytes: int = 0) -> Job:
    job = Job(job_id=job_id, tool_name=tool_name, question=question, filename=filename,
              file_bytes=file_bytes)
    with _LOCK:
        _JOBS[job_id] = job
        while len(_JOBS) > _MAX_JOBS:
            _JOBS.popitem(last=False)
    vlog.log("─" * 72)
    vlog.log(f"▶ JOB START  tool={tool_name}  file={filename!r} ({file_bytes:,}B)  "
             f"q={vlog.short(question,120)!r}")
    db.record_job_start(job)
    return job


def emit(job_id: str, step: str, status: str = "running", detail: str = "",
         elapsed_ms: int | None = None, **data) -> None:
    """Append an event to the job, log it, and persist it. `step` may be 'analyze:logv' etc."""
    ev = JobEvent(step=step, status=status, detail=detail, elapsed_ms=elapsed_ms, data=data)
    seq = 0
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.events.append(ev)
            seq = len(job.events)
    icon = {"running": "·", "ok": "✔", "skipped": "⤼", "error": "✖"}.get(status, "·")
    ms = f" ({elapsed_ms}ms)" if elapsed_ms is not None else ""
    lvl = vlog.WARNING if status == "error" else vlog.INFO
    vlog.log(f"  {icon} [{step}] {status}{ms} {detail}", lvl)
    db.record_event(job_id, seq, ev)


def set_meta(job_id: str, **fields) -> None:
    """Update job-level fields (routing, AI Controller, ORB, …) as the pipeline learns them."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)


def add_analyzer_run(job_id: str, run: AnalyzerRun, http_status: int | None = None) -> None:
    """Record one analyzer's full story on this job (config → discovery → request → result)."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            existing = next((i for i, a in enumerate(job.analyzers)
                             if a.analyzer_id == run.analyzer_id), None)
            if existing is None:
                job.analyzers.append(run)
            else:
                job.analyzers[existing] = run
    db.record_analyzer(job_id, run, http_status)


def finish(job_id: str, status: str = "done", report_chars: int = 0, error: str | None = None,
           selected: list[str] | None = None, orb_enabled: bool = False,
           report_markdown: str = "") -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.status = status
            job.finished_at = time.time()
            job.report_chars = report_chars
            job.report_markdown = report_markdown
            job.error = error
            if selected is not None:
                job.selected_analyzers = selected
            job.orb_enabled = orb_enabled
        dur = (f" in {job.finished_at - job.started_at:.1f}s"
               if job and job.finished_at else "")
    vlog.log(f"◀ JOB {status.upper()}  report={report_chars:,} chars{dur}"
             + (f"  error={error}" if error else ""),
             vlog.WARNING if status == "error" else vlog.INFO)
    if job is not None:
        db.record_job_finish(job)


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def recent(limit: int = 50) -> list[Job]:
    with _LOCK:
        return list(_JOBS.values())[-limit:][::-1]  # newest first
