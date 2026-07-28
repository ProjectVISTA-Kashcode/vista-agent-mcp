"""The VISTA-MCP flow GUI — an n8n-style live view of the orchestrator, in-process.

It runs inside the MCP server process (so it reads the SAME in-memory job registry the
pipeline writes to) and is attached with :func:`register` via FastMCP custom routes. It serves:

    GET  /gui                     the single-page app (gui/index.html)
    GET  /gui/api/state           tools (from TOOL_ENABLEMENT) + recent job summaries  (polled)
    GET  /gui/api/jobs/{job_id}   one job's full flow (ordered nodes + statuses + timings)
    GET  /gui/api/config          the raw TOOL_ENABLEMENT JSON (for the editor)
    POST /gui/api/config          save the TOOL_ENABLEMENT JSON (validated, hot-reloaded)

The flow the GUI draws is DERIVED from the job's events + the tool's config, so it is
"different for different tools": the analyzer nodes are exactly that tool's analyzers, and the
ORB node appears only when the tool enables it.

Security note: these routes are NOT behind the MCP bearer token — this is a local operator
console. Bind the server to localhost (or a trusted network) in production, or front it with
your own auth. The config POST is the only mutating route and it validates before writing.
"""
from __future__ import annotations

import json
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import jobs as jobs_mod
from . import tool_enablement
from .models import Job

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_INDEX = os.path.join(_REPO, "gui", "index.html")

# The canonical, tool-independent backbone of the flow. Analyzer nodes are spliced in between
# "decide" and "concat" per tool/job; "orb" is dropped when the tool disables it.
_BACKBONE = ["intake", "vfr", "tool_enablement", "discover", "decide", "concat", "orb", "done"]


def _meta(step: str) -> tuple[str, str]:
    """(icon, label) for a step key, handling 'analyze:<id>'."""
    base = step.split(":", 1)[0]
    icon, label = jobs_mod.STEP_META.get(base, ("•", base))
    if step.startswith("analyze:"):
        return icon, f"Analyzer · {step.split(':', 1)[1]}"
    return icon, label


def _latest_for_step(job: Job, step: str) -> dict | None:
    """The most recent event for a step key (later events supersede earlier)."""
    found = None
    for ev in job.events:
        if ev.step == step:
            found = ev
    if found is None:
        return None
    return {
        "status": found.status,
        "detail": found.detail,
        "elapsed_ms": found.elapsed_ms,
        "data": found.data,
    }


def _analyzer_steps(job: Job) -> list[str]:
    """Ordered, de-duped 'analyze:<id>' step keys seen on this job."""
    seen: list[str] = []
    for ev in job.events:
        if ev.step.startswith("analyze:") and ev.step not in seen:
            seen.append(ev.step)
    return seen


def _flow_nodes(job: Job) -> list[dict]:
    """Ordered flow nodes with live status for one job (what the canvas renders)."""
    cfg = tool_enablement.get(job.tool_name)
    order = ["intake", "vfr", "tool_enablement", "discover", "decide",
             *_analyzer_steps(job), "concat", "orb", "done"]
    # drop the ORB node entirely if this tool never runs ORB
    if cfg is not None and not cfg.orb_enabled:
        order = [s for s in order if s != "orb"]

    nodes = []
    for step in order:
        icon, label = _meta(step)
        ev = _latest_for_step(job, step)
        nodes.append({
            "key": step,
            "icon": icon,
            "label": label,
            "status": ev["status"] if ev else "pending",
            "detail": ev["detail"] if ev else "",
            "elapsed_ms": ev["elapsed_ms"] if ev else None,
            "parallel": step.startswith("analyze:"),
        })
    return nodes


def _tool_template(tool_name: str) -> dict:
    """The static shape of a tool's flow (shown before/without a selected job)."""
    cfg = tool_enablement.get(tool_name)
    analyzers = []
    if cfg is not None:
        for a in cfg.analyzers:
            analyzers.append({
                "id": a.id, "title": a.title or a.id,
                "mandatory": a.mandatory, "enabled": a.enabled,
                "api_url": a.api_url,
            })
    return {
        "tool_name": tool_name,
        "description": (cfg.description if cfg else ""),
        "routing_system_prompt": (cfg.routing_system_prompt if cfg else ""),
        "orb_enabled": (cfg.orb_enabled if cfg else False),
        "analyzers": analyzers,
    }


def _job_summary(job: Job) -> dict:
    dur = (job.finished_at - job.started_at) if job.finished_at else None
    # count the last-known status of each step for a compact progress read
    return {
        "job_id": job.job_id,
        "tool_name": job.tool_name,
        "question": job.question,
        "filename": job.filename,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_s": round(dur, 1) if dur is not None else None,
        "selected_analyzers": job.selected_analyzers,
        "orb_enabled": job.orb_enabled,
        "report_chars": job.report_chars,
        "error": job.error,
        "event_count": len(job.events),
    }


# --------------------------------------------------------------------------- #
# Route handlers
# --------------------------------------------------------------------------- #
async def index(_request: Request) -> HTMLResponse:
    try:
        with open(_INDEX, encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>VISTA-MCP GUI</h1><p>gui/index.html not found.</p>", status_code=500)


async def api_state(request: Request) -> JSONResponse:
    limit = int(request.query_params.get("limit", "60"))
    tools = {name: _tool_template(name) for name in tool_enablement.load().keys()}
    job_list = [_job_summary(j) for j in jobs_mod.recent(limit)]
    return JSONResponse({"tools": tools, "jobs": job_list})


async def api_job(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    job = jobs_mod.get(job_id)
    if job is None:
        return JSONResponse({"error": f"job '{job_id}' not found"}, status_code=404)
    return JSONResponse({
        "summary": _job_summary(job),
        "nodes": _flow_nodes(job),
        "events": [
            {"step": e.step, "status": e.status, "detail": e.detail,
             "at": e.at, "elapsed_ms": e.elapsed_ms, "data": e.data}
            for e in job.events
        ],
    })


async def api_config_get(_request: Request) -> JSONResponse:
    return JSONResponse(tool_enablement.raw_json())


async def api_config_post(request: Request) -> JSONResponse:
    try:
        new_raw = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "body is not valid JSON"}, status_code=400)
    if not isinstance(new_raw, dict) or "tools" not in new_raw:
        return JSONResponse(
            {"ok": False, "error": 'config must be a JSON object with a "tools" key'},
            status_code=400,
        )
    try:
        parsed = tool_enablement.save_json(new_raw)  # validates by re-parsing, then persists
    except Exception as e:  # noqa: BLE001 — invalid config → keep the old file, report why
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse({"ok": True, "tools": sorted(parsed.keys())})


def register(mcp) -> None:
    """Attach the GUI routes to a FastMCP server.

    Mounted under BOTH ``/gui`` (direct) and ``/mcp/gui`` (so it's reachable behind a reverse
    proxy that only forwards ``/mcp/*`` to this server — the production layout at
    ``https://vista.fortinet.com/mcp/``). The front-end derives its API base from its own URL, so
    the same page works at either prefix.
    """
    for pfx in ("/gui", "/mcp/gui"):
        mcp.custom_route(pfx, methods=["GET"])(index)
        mcp.custom_route(pfx + "/", methods=["GET"])(index)
        mcp.custom_route(pfx + "/api/state", methods=["GET"])(api_state)
        mcp.custom_route(pfx + "/api/jobs/{job_id}", methods=["GET"])(api_job)
        mcp.custom_route(pfx + "/api/config", methods=["GET"])(api_config_get)
        mcp.custom_route(pfx + "/api/config", methods=["POST"])(api_config_post)
