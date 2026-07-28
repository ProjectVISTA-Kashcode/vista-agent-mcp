"""The orchestrator pipeline — the modular, dynamic core of VISTA-MCP.

For one tool call it runs the flow from the architecture diagram:

    file → VFR → TOOL_ENABLEMENT → DISCOVER TOOLS → DEEPSEEK DECISION
         → call selected analyzers (ASYNC, in parallel) → CONCATENATE
         → ORB (if enabled) appended → answer back

Everything is config-driven (``config/tool_enablement.json``) and generic (one function calls
any analyzer, one function reads any result), so **adding an analyzer is a config edit, not
code**. Each step is logged to the terminal and recorded on a :class:`~orchestrator.models.Job`
for the flow GUI.

Concurrency: each tool call owns its own ``job_id`` and its own async context, so the server
handles many clients/files at once; selected analyzers within a call run concurrently.
"""
from __future__ import annotations

import asyncio
import time

import vlog

from . import decide, discovery, jobs, orb, tool_enablement, vfr
from .analyzer_client import call as call_analyzer
from .models import AnalyzerDiscovery, AnalyzerRef, AnalyzerResult


async def _call_one(job_id: str, ref: AnalyzerRef, disc: AnalyzerDiscovery,
                    file_bytes: bytes, filename: str, question: str) -> AnalyzerResult:
    step = f"analyze:{ref.id}"
    jobs.emit(job_id, step, "running", f"{ref.title or ref.id}")
    t0 = time.time()
    result = await call_analyzer(ref, disc, file_bytes, filename, question)
    ms = int((time.time() - t0) * 1000)
    if result.ok:
        jobs.emit(job_id, step, "ok",
                  f"{len(result.report_markdown):,} chars"
                  + (f" (log_type={result.meta.get('log_type')})" if result.meta.get("log_type") else ""),
                  elapsed_ms=ms)
    else:
        jobs.emit(job_id, step, "error",
                  (result.error or {}).get("message", "failed"), elapsed_ms=ms)
    return result


async def run(tool_name: str, file_bytes: bytes, filename: str, question: str = "",
              job_id: str | None = None) -> str:
    """Run the full pipeline for one MCP tool call and return the final markdown report."""
    job_id = job_id or vlog.new_rid()
    jobs.start(job_id, tool_name, question, filename)

    try:
        # 0) INTAKE — the tool call as received + the fetched file (the first flow node, so each
        #    execution shows what was called and which file came in).
        jobs.emit(job_id, "intake", "ok",
                  f"tool={tool_name} · file='{filename}' ({len(file_bytes):,} bytes)"
                  + (f" · q=\"{question[:80]}\"" if question else " · (no question)"),
                  filename=filename, bytes=len(file_bytes), question=question)

        # 1) VFR — passthrough today; a real seam for later routing logic.
        jobs.emit(job_id, "vfr", "running")
        vfr_res = vfr.run(file_bytes, filename)
        file_bytes, filename = vfr_res.file_bytes, vfr_res.filename
        jobs.emit(job_id, "vfr", "ok", vfr_res.message)

        # 2) TOOL_ENABLEMENT — the static config for this tool.
        jobs.emit(job_id, "tool_enablement", "running")
        cfg = tool_enablement.get(tool_name)
        if cfg is None or not cfg.analyzers:
            jobs.emit(job_id, "tool_enablement", "error", f"no config for tool '{tool_name}'")
            jobs.finish(job_id, "error", error="no tool config")
            return f"⚠️ No analyzers are configured for '{tool_name}'."
        mandatory = cfg.mandatory()
        optional = cfg.optional()
        jobs.emit(job_id, "tool_enablement", "ok",
                  f"mandatory={[r.id for r in mandatory]}  optional={[r.id for r in optional]}  "
                  f"orb={'on' if cfg.orb_enabled else 'off'}",
                  mandatory=[r.id for r in mandatory], optional=[r.id for r in optional],
                  orb_enabled=cfg.orb_enabled)

        # 3) DISCOVER TOOLS — query each analyzer's /discover (async, fail-soft).
        jobs.emit(job_id, "discover", "running")
        refs = mandatory + optional
        discoveries = await discovery.discover(refs)
        mandatory_ok = [r for r in mandatory if r.id in discoveries]
        optional_ok = [r for r in optional if r.id in discoveries]
        dropped = [r.id for r in refs if r.id not in discoveries]
        jobs.emit(job_id, "discover",
                  "ok" if discoveries else "error",
                  f"discovered={list(discoveries)}"
                  + (f"  dropped={dropped}" if dropped else ""),
                  discovered=list(discoveries), dropped=dropped)
        if not mandatory_ok and not optional_ok:
            jobs.finish(job_id, "error", error="no analyzers reachable")
            return "⚠️ No analyzers could be reached to handle this request."

        # 4) DEEPSEEK DECISION — skip when there are no optional analyzers. The routing system
        #    prompt is per-tool (config-driven), so each MCP tool routes with its own guidance.
        jobs.emit(job_id, "decide", "running")
        decision = await decide.decide(
            question, mandatory_ok, optional_ok, discoveries,
            routing_system_prompt=cfg.routing_system_prompt,
        )
        jobs.emit(job_id, "decide", "skipped" if not decision.used_deepseek else "ok",
                  decision.reason, used_deepseek=decision.used_deepseek,
                  selected=decision.selected_ids, system_prompt=decision.system_prompt)

        # 5) CALL selected analyzers — ASYNC, in parallel.
        selected_refs = [r for r in refs if r.id in decision.selected_ids and r.id in discoveries]
        vlog.log(f"  ▶ calling {len(selected_refs)} analyzer(s) in parallel: "
                 f"{[r.id for r in selected_refs]}")
        results = await asyncio.gather(*[
            _call_one(job_id, r, discoveries[r.id], file_bytes, filename, question)
            for r in selected_refs
        ])

        # 6) CONCATENATE the analyzer reports.
        jobs.emit(job_id, "concat", "running")
        sections = [r.report_markdown for r in results if r and r.report_markdown.strip()]
        combined = "\n\n---\n\n".join(sections) if sections else \
            "⚠️ No analyzer produced a result for this request."
        jobs.emit(job_id, "concat", "ok",
                  f"{len(sections)} section(s), {len(combined):,} chars")

        # 7) ORB — static config; runs once at the end if enabled.
        if cfg.orb_enabled:
            jobs.emit(job_id, "orb", "running")
            orb_answer = await orb.fetch(combined, question)
            if orb_answer:
                combined += f"\n\n{orb.ORB_SECTION_HEADING}\n\n{orb_answer}"
                jobs.emit(job_id, "orb", "ok", f"{len(orb_answer):,} chars appended")
            else:
                jobs.emit(job_id, "orb", "skipped", "no ORB output (fail-open)")
        else:
            jobs.emit(job_id, "orb", "skipped", "ORB disabled in config")

        # 8) DONE.
        jobs.emit(job_id, "done", "ok", f"{len(combined):,} chars")
        jobs.finish(job_id, "done", report_chars=len(combined),
                    selected=decision.selected_ids, orb_enabled=cfg.orb_enabled)
        return combined

    except Exception as e:  # noqa: BLE001 — never leak a raw stack to the MCP client
        vlog.log(f"✖ pipeline crashed: {type(e).__name__}: {e}", vlog.ERROR)
        jobs.finish(job_id, "error", error=f"{type(e).__name__}: {e}")
        return "⚠️ The analysis pipeline encountered an internal error."
