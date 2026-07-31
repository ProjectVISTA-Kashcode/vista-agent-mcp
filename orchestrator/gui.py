"""The VISTA-MCP operator console — live flow, dashboard analytics, and durable history.

It runs inside the MCP server process (so it reads the SAME in-memory job registry the pipeline
writes to, with no polling of files) and is attached with :func:`register` via FastMCP custom
routes. It serves:

    GET  /gui                       the single-page app (gui/index.html)
    GET  /gui/user-guide            standalone "how to use this console" page (own tab)
    GET  /gui/api-integration-guide standalone "how to plug an analyzer in" page (own tab)
    GET  /gui/api/state             tools (TOOL_ENABLEMENT) + recent jobs + DB/server status
    GET  /gui/api/jobs              durable history from Postgres (filtered + paginated)
    GET  /gui/api/jobs/{job_id}     one job's full flow — live from memory, else from Postgres
    GET  /gui/api/analytics         dashboard aggregates (totals, series, per tool/analyzer)
    GET  /gui/api/facets            distinct tools/analyzers seen, for the history filters
    GET  /gui/api/config            the raw TOOL_ENABLEMENT JSON (for the editor)
    POST /gui/api/config            save it (validated, hot-reloaded, audited to the DB)
    POST /gui/api/probe             fetch an analyzer's /discover so the editor can auto-fill

The flow the GUI draws is DERIVED from the job's events + the tool's config, so it is
"different for different tools": the analyzer nodes are exactly that tool's analyzers, the AI
Controller node carries the prompt/decision/plans it produced, and the ORB node appears only
when the tool enables it.

Security note: these routes are NOT behind the MCP bearer token — this is a local operator
console. Bind the server to localhost (or a trusted network) in production, or front it with
your own auth. ``/api/config`` (write) and ``/api/probe`` (server-side GET to the URL you type)
are the only non-read routes; both validate their input.
"""
from __future__ import annotations

import asyncio
import os
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import ai_controller, db
from . import discovery as discovery_mod
from . import jobs as jobs_mod
from . import orb as orb_mod
from . import tool_enablement
from .models import Job

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_GUI_DIR = os.path.join(_REPO, "gui")

# The flow backbone is tool-independent: intake → vfr → tool_enablement → discover →
# ai_controller → [that job's analyzers, in parallel] → concat → orb → done. Analyzer nodes are
# spliced in per job; "orb" is dropped when the tool disables it. See _flow_nodes().


def _meta(step: str) -> tuple[str, str]:
    """(icon, label) for a step key, handling 'analyze:<id>'."""
    base = step.split(":", 1)[0]
    icon, label = jobs_mod.STEP_META.get(base, ("•", base))
    if step.startswith("analyze:"):
        return icon, f"Analyzer · {step.split(':', 1)[1]}"
    return icon, label


def _latest_for_step(events: list[dict], step: str) -> dict | None:
    """The most recent event for a step key (later events supersede earlier)."""
    found = None
    for ev in events:
        if ev["step"] == step:
            found = ev
    return found


def _analyzer_steps(events: list[dict]) -> list[str]:
    """Ordered, de-duped 'analyze:<id>' step keys seen on this job."""
    seen: list[str] = []
    for ev in events:
        if ev["step"].startswith("analyze:") and ev["step"] not in seen:
            seen.append(ev["step"])
    return seen


def _event_dicts(job: Job) -> list[dict]:
    return [{"step": e.step, "status": e.status, "detail": e.detail, "at": e.at,
             "elapsed_ms": e.elapsed_ms, "data": e.data} for e in job.events]


def _flow_nodes(events: list[dict], tool_name: str, analyzers: list[dict],
                orb_enabled: bool | None = None) -> list[dict]:
    """Ordered flow nodes with live status (what the canvas renders)."""
    cfg = tool_enablement.get(tool_name)
    order = ["intake", "vfr", "tool_enablement", "discover", "ai_controller",
             *_analyzer_steps(events), "concat", "orb", "done"]
    has_orb_event = any(e["step"] == "orb" for e in events)
    show_orb = has_orb_event if orb_enabled is None else (orb_enabled or has_orb_event)
    if cfg is not None and not cfg.orb_enabled and not has_orb_event:
        show_orb = False
    if not show_orb:
        order = [s for s in order if s != "orb"]

    by_id = {a["analyzer_id"]: a for a in analyzers}
    nodes = []
    for step in order:
        icon, label = _meta(step)
        ev = _latest_for_step(events, step)
        node = {
            "key": step,
            "icon": icon,
            "label": label,
            "status": ev["status"] if ev else "pending",
            "detail": ev["detail"] if ev else "",
            "elapsed_ms": ev["elapsed_ms"] if ev else None,
            "data": ev["data"] if ev else {},
            "parallel": step.startswith("analyze:"),
        }
        if step.startswith("analyze:"):
            aid = step.split(":", 1)[1]
            a = by_id.get(aid)
            if a:
                node["tag"] = "mand" if a.get("mandatory") else "opt"
                node["analyzer"] = a
                node["label"] = f"Analyzer · {a.get('title') or aid}"
        nodes.append(node)
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
                "api_url": a.api_url, "discover_url": a.discover_url, "timeout": a.timeout,
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
    return {
        "job_id": job.job_id,
        "tool_name": job.tool_name,
        "question": job.question,
        "filename": job.filename,
        "file_bytes": job.file_bytes,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_s": round(dur, 1) if dur is not None else None,
        "selected_analyzers": job.selected_analyzers,
        "mandatory_ids": job.mandatory_ids,
        "optional_ids": job.optional_ids,
        "discovered_ids": job.discovered_ids,
        "dropped_ids": job.dropped_ids,
        "used_ai": job.used_ai,
        "ai_mode": job.ai_mode,
        "ai_reason": job.ai_reason,
        "ai_system_prompt": job.ai_system_prompt,
        "ai_raw": job.ai_raw,
        "ai_plan_source": job.ai_plan_source,
        "ai_elapsed_ms": job.ai_elapsed_ms,
        "orb_enabled": job.orb_enabled,
        "orb_status": job.orb_status,
        "orb_chars": job.orb_chars,
        "orb_elapsed_ms": job.orb_elapsed_ms,
        "report_chars": job.report_chars,
        "error": job.error,
        "event_count": len(job.events),
        "live": True,
    }


def _analyzer_dicts(job: Job) -> list[dict]:
    out = []
    for a in job.analyzers:
        d = a.model_dump(mode="json")
        d["plan"] = a.plan.model_dump(mode="json") if a.plan else None
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Route handlers
# --------------------------------------------------------------------------- #
def _page(filename: str) -> HTMLResponse:
    try:
        with open(os.path.join(_GUI_DIR, filename), encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    except FileNotFoundError:
        return HTMLResponse(f"<h1>VISTA-MCP</h1><p>gui/{filename} not found.</p>", status_code=500)


async def index(_request: Request) -> HTMLResponse:
    return _page("index.html")


async def user_guide(_request: Request) -> HTMLResponse:
    """Standalone page: how to use this console (opened in its own tab)."""
    return _page("user_guide.html")


async def api_guide(_request: Request) -> HTMLResponse:
    """Standalone page: the API an analyzer must expose to plug into this MCP server."""
    return _page("api_guide.html")


async def api_state(request: Request) -> JSONResponse:
    """Tools + the most recent jobs + subsystem status.

    The job list is **live jobs first, then the database**, de-duplicated and sorted newest-first.
    So the sidebar keeps showing real history across a restart (and jobs written by another
    instance), not just what this process happens to hold in memory.
    """
    limit = max(1, min(int(request.query_params.get("limit", "30")), 200))
    tools = {name: _tool_template(name) for name in tool_enablement.load().keys()}

    live = [_job_summary(j) for j in jobs_mod.recent(limit)]
    live_ids = {j["job_id"] for j in live}
    stored: list[dict] = []
    total = len(live)
    if db.status().get("connected"):
        page = await asyncio.to_thread(db.list_jobs, {}, limit, 0)
        stored = [j for j in page.get("jobs", []) if j["job_id"] not in live_ids]
        total = max(page.get("total", 0), len(live))
    job_list = sorted(live + stored, key=lambda j: j.get("started_at") or 0, reverse=True)[:limit]

    return JSONResponse({
        "tools": tools,
        "jobs": job_list,
        "jobs_total": total,
        "db": db.status(),
        "server": {
            "ai_controller": {
                "enabled": ai_controller.ENABLED,
                "url": ai_controller.GEN_URL,
                "timeout": ai_controller.TIMEOUT,
            },
            "orb_url": orb_mod.ORB_ASK_URL,
            "time": time.time(),
        },
    })


def _filters(request: Request) -> dict:
    qp = request.query_params
    f = {k: qp.get(k) for k in ("tool", "status", "analyzer", "q", "orb", "ai", "bucket")}
    if qp.get("since_hours"):
        try:
            f["since_hours"] = max(1, min(int(qp["since_hours"]), 24 * 365 * 5))
        except ValueError:
            pass
    return {k: v for k, v in f.items() if v}


async def api_jobs(request: Request) -> JSONResponse:
    """Durable job history from Postgres (the History view)."""
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        limit, offset = 50, 0
    data = await asyncio.to_thread(db.list_jobs, _filters(request), limit, offset)
    data["limit"] = limit
    data["offset"] = offset
    return JSONResponse(data)


async def api_job(request: Request) -> JSONResponse:
    """One job: live from memory while it runs, from Postgres forever after."""
    job_id = request.path_params["job_id"]
    job = jobs_mod.get(job_id)
    if job is not None:
        events = _event_dicts(job)
        analyzers = _analyzer_dicts(job)
        return JSONResponse({
            "summary": _job_summary(job),
            "nodes": _flow_nodes(events, job.tool_name, analyzers, job.orb_enabled),
            "events": events,
            "analyzers": analyzers,
            "report_markdown": job.report_markdown,
            "source": "live",
        })

    stored = await asyncio.to_thread(db.get_job, job_id)
    if stored is None:
        return JSONResponse({"error": f"job '{job_id}' not found"}, status_code=404)
    summary = stored["summary"]
    summary["live"] = False
    summary["event_count"] = len(stored["events"])
    return JSONResponse({
        "summary": summary,
        "nodes": _flow_nodes(stored["events"], summary["tool_name"], stored["analyzers"],
                             summary.get("orb_enabled")),
        "events": stored["events"],
        "analyzers": stored["analyzers"],
        "report_markdown": stored.get("report_markdown", ""),
        "source": "db",
    })


async def api_analytics(request: Request) -> JSONResponse:
    data = await asyncio.to_thread(db.analytics, _filters(request))
    return JSONResponse(data)


async def api_facets(_request: Request) -> JSONResponse:
    data = await asyncio.to_thread(db.facets)
    return JSONResponse(data)


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
    actor = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
    db.record_config_save(sorted(parsed.keys()), new_raw, actor=actor or "gui")
    return JSONResponse({"ok": True, "tools": sorted(parsed.keys())})


async def api_probe(request: Request) -> JSONResponse:
    """Fetch an analyzer's ``/discover`` so the editor can validate + auto-fill it."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    url = str((body or {}).get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "no url given"}, status_code=400)
    disc, err = await discovery_mod.probe(url)
    if disc is None:
        return JSONResponse({"ok": False, "error": err})
    return JSONResponse({
        "ok": True,
        "analyzer": disc.analyzer.model_dump(mode="json"),
        "query": disc.query.model_dump(mode="json"),
        "discovery": disc.model_dump(mode="json"),
    })


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
        # standalone documentation pages (opened in their own tab from the header)
        mcp.custom_route(pfx + "/user-guide", methods=["GET"])(user_guide)
        mcp.custom_route(pfx + "/api-integration-guide", methods=["GET"])(api_guide)
        mcp.custom_route(pfx + "/api/state", methods=["GET"])(api_state)
        mcp.custom_route(pfx + "/api/jobs", methods=["GET"])(api_jobs)
        mcp.custom_route(pfx + "/api/jobs/{job_id}", methods=["GET"])(api_job)
        mcp.custom_route(pfx + "/api/analytics", methods=["GET"])(api_analytics)
        mcp.custom_route(pfx + "/api/facets", methods=["GET"])(api_facets)
        mcp.custom_route(pfx + "/api/config", methods=["GET"])(api_config_get)
        mcp.custom_route(pfx + "/api/config", methods=["POST"])(api_config_post)
        mcp.custom_route(pfx + "/api/probe", methods=["POST"])(api_probe)
