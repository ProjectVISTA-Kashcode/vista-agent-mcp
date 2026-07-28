"""Job + flow tracking — the source of truth for the CLI logs AND the flow GUI.

Every tool invocation is a :class:`~orchestrator.models.Job`. Each pipeline step appends a
:class:`~orchestrator.models.JobEvent` (pfr → tool_enablement → discover → decide →
analyze:<id> … → orb → concat → done). ``emit()`` does two things at once:

  * writes a structured line to the terminal via ``vlog`` (extensive CLI logging), and
  * stores the event on the in-memory job so the GUI can render the live n8n-style flow.

The registry is bounded (keeps the most recent N jobs) and thread/async-safe enough for
concurrent tool calls — each call owns its own job id.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

import vlog

from .models import Job, JobEvent

# Keep only the most recent jobs in memory; older ones are evicted. This is intentionally small
# (a live operator view). Durable history/metrics belong in a DB + Grafana later, not here.
_MAX_JOBS = 30
_JOBS: "OrderedDict[str, Job]" = OrderedDict()
_LOCK = threading.Lock()

# per-step display metadata for the GUI (icon + human label)
STEP_META = {
    "intake": ("📥", "Tool Call & Fetch"),
    "vfr": ("🧭", "VFR (routing)"),
    "tool_enablement": ("⚙️", "Tool Enablement"),
    "discover": ("🔎", "Discover Tools"),
    "decide": ("🧠", "DeepSeek Decision"),
    "analyze": ("📊", "Analyzer"),
    "orb": ("🛟", "ORB Suggestions"),
    "concat": ("🧩", "Concatenate"),
    "done": ("✅", "Answer & Back"),
}


def start(job_id: str, tool_name: str, question: str, filename: str) -> Job:
    job = Job(job_id=job_id, tool_name=tool_name, question=question, filename=filename)
    with _LOCK:
        _JOBS[job_id] = job
        while len(_JOBS) > _MAX_JOBS:
            _JOBS.popitem(last=False)
    vlog.log("─" * 72)
    vlog.log(f"▶ JOB START  tool={tool_name}  file={filename!r}  q={vlog.short(question,120)!r}")
    return job


def emit(job_id: str, step: str, status: str = "running", detail: str = "",
         elapsed_ms: int | None = None, **data) -> None:
    """Append an event to the job and log it. `step` may be 'analyze:logv' etc."""
    ev = JobEvent(step=step, status=status, detail=detail, elapsed_ms=elapsed_ms, data=data)
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.events.append(ev)
    icon = {"running": "·", "ok": "✔", "skipped": "⤼", "error": "✖"}.get(status, "·")
    ms = f" ({elapsed_ms}ms)" if elapsed_ms is not None else ""
    lvl = vlog.WARNING if status == "error" else vlog.INFO
    vlog.log(f"  {icon} [{step}] {status}{ms} {detail}", lvl)


def finish(job_id: str, status: str = "done", report_chars: int = 0, error: str | None = None,
           selected: list[str] | None = None, orb_enabled: bool = False) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.status = status
            job.finished_at = time.time()
            job.report_chars = report_chars
            job.error = error
            if selected is not None:
                job.selected_analyzers = selected
            job.orb_enabled = orb_enabled
    dur = ""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job and job.finished_at:
            dur = f" in {job.finished_at - job.started_at:.1f}s"
    vlog.log(f"◀ JOB {status.upper()}  report={report_chars:,} chars{dur}"
             + (f"  error={error}" if error else ""),
             vlog.WARNING if status == "error" else vlog.INFO)


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def recent(limit: int = 50) -> list[Job]:
    with _LOCK:
        return list(_JOBS.values())[-limit:][::-1]  # newest first
