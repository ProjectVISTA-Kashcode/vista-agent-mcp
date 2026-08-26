"""The orchestrator pipeline — the modular, dynamic core of VISTA-MCP.

For one tool call it runs the flow from the architecture diagram:

    file → VFR → TOOL_ENABLEMENT → DISCOVER TOOLS → AI CONTROLLER
         → call selected analyzers (ASYNC, in parallel, each via its planned request)
         → CONCATENATE → ORB (if enabled) appended → answer back

Everything is config-driven (``config/tool_enablement.json``) and generic (one function calls
any analyzer, one function reads any result), so **adding an analyzer is a config edit, not
code**. The AI Controller step runs on **every** job: it reads each analyzer's freshly-fetched
discovery document and decides both *which* analyzers to run and *how* to call each one, so an
analyzer that changes its API — or a brand-new analyzer added minutes ago — is used correctly
without a code change or a restart.

Each step is logged to the terminal, recorded on a :class:`~orchestrator.models.Job` for the
flow GUI, and persisted to Postgres for the Dashboard/History views.

Concurrency: each tool call owns its own ``job_id`` and its own async context, so the server
handles many clients/files at once; selected analyzers within a call run concurrently.
"""
from __future__ import annotations

import asyncio
import time

import vlog

from . import decide, discovery, jobs, orb, plan as plan_mod, tool_enablement, vfr
from .analyzer_client import call as call_analyzer
from .models import AnalyzerDiscovery, AnalyzerRef, AnalyzerResult, AnalyzerRun, CallPlan


def _run_record(ref: AnalyzerRef) -> AnalyzerRun:
    return AnalyzerRun(analyzer_id=ref.id, title=ref.title or ref.id, api_url=ref.api_url,
                       mandatory=ref.mandatory)


async def _call_one(job_id: str, ref: AnalyzerRef, disc: AnalyzerDiscovery, plan: CallPlan,
                    file_bytes: bytes, filename: str, question: str,
                    record: AnalyzerRun) -> AnalyzerResult:
    step = f"analyze:{ref.id}"
    jobs.emit(job_id, step, "running", f"{ref.title or ref.id} · {plan_mod.summarize(plan)}",
              plan=plan.model_dump(mode="json"))
    t0 = time.time()
    result, http_status = await call_analyzer(ref, disc, file_bytes, filename, question, plan)
    ms = int((time.time() - t0) * 1000)

    record.called = True
    record.ok = bool(result.ok)
    record.elapsed_ms = ms
    record.report_chars = len(result.report_markdown)
    record.report_markdown = result.report_markdown
    record.meta = dict(result.meta or {})
    record.error = (result.error or {}).get("message", "") if result.error else ""

    if result.ok:
        jobs.emit(job_id, step, "ok",
                  f"{len(result.report_markdown):,} chars"
                  + (f" (log_type={result.meta.get('log_type')})" if result.meta.get("log_type") else ""),
                  elapsed_ms=ms, http_status=http_status, plan_source=plan.source,
                  request_url=plan.url, meta=dict(result.meta or {}))
    else:
        jobs.emit(job_id, step, "error", record.error or "failed",
                  elapsed_ms=ms, http_status=http_status, plan_source=plan.source,
                  request_url=plan.url)
    jobs.add_analyzer_run(job_id, record, http_status)
    return result


async def run(tool_name: str, file_bytes: bytes, filename: str, question: str = "",
              job_id: str | None = None) -> str:
    """Run the full pipeline for one MCP tool call and return the final markdown report."""
    job_id = job_id or vlog.new_rid()
    jobs.start(job_id, tool_name, question, filename, len(file_bytes))

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
        jobs.emit(job_id, "vfr", "ok", vfr_res.message, matched=vfr_res.matched)

        # 2) TOOL_ENABLEMENT — the static config for this tool.
        jobs.emit(job_id, "tool_enablement", "running")
        cfg = tool_enablement.get(tool_name)
        if cfg is None or not cfg.analyzers:
            jobs.emit(job_id, "tool_enablement", "error", f"no config for tool '{tool_name}'")
            jobs.finish(job_id, "error", error="no tool config")
            return f"⚠️ No analyzers are configured for '{tool_name}'."
        mandatory = cfg.mandatory()
        optional = cfg.optional()
        refs = mandatory + optional
        records: dict[str, AnalyzerRun] = {r.id: _run_record(r) for r in refs}
        jobs.set_meta(job_id, mandatory_ids=[r.id for r in mandatory],
                      optional_ids=[r.id for r in optional], orb_enabled=cfg.orb_enabled)
        jobs.emit(job_id, "tool_enablement", "ok",
                  f"mandatory={[r.id for r in mandatory]}  optional={[r.id for r in optional]}  "
                  f"orb={'on' if cfg.orb_enabled else 'off'}",
                  mandatory=[r.id for r in mandatory], optional=[r.id for r in optional],
                  orb_enabled=cfg.orb_enabled,
                  routing_prompt="custom" if cfg.routing_system_prompt.strip() else "default")

        # 3) DISCOVER TOOLS — query each analyzer's /discover (async, fail-soft). This is where
        #    an analyzer's CURRENT contract comes from; nothing about it is assumed here.
        jobs.emit(job_id, "discover", "running", f"{len(refs)} config entry(s) · concurrent")
        discoveries, runtime_refs, dropped = await discovery.discover(refs)
        # A CATALOG entry (one /discover advertising several analyzers, as ORB does) expands into
        # several runtime analyzers, so the set that actually runs is not the set in the config.
        # Re-seed the records off the runtime refs, keeping a record for each config entry that
        # failed discovery so history still shows why it dropped out.
        records = {r.id: _run_record(r) for r in refs if r.id in dropped}
        for r in runtime_refs:
            records[r.id] = _run_record(r)
        refs = runtime_refs
        mandatory_ok = [r for r in runtime_refs if r.mandatory]
        optional_ok = [r for r in runtime_refs if not r.mandatory]
        if [r.id for r in runtime_refs] != [r.id for r in mandatory + optional if r.id not in dropped]:
            jobs.set_meta(job_id, mandatory_ids=[r.id for r in mandatory_ok],
                          optional_ids=[r.id for r in optional_ok])
        for rid, disc in discoveries.items():
            rec = records.get(rid)
            if rec is not None:
                rec.discovered = True
                rec.when_to_use = disc.analyzer.when_to_use
                rec.discovery = disc.model_dump(mode="json")
                if disc.analyzer.title:
                    rec.title = rec.title or disc.analyzer.title
        jobs.set_meta(job_id, discovered_ids=list(discoveries), dropped_ids=dropped)
        jobs.emit(job_id, "discover",
                  "ok" if discoveries else "error",
                  f"discovered={list(discoveries)}"
                  + (f"  dropped={dropped}" if dropped else ""),
                  discovered=list(discoveries), dropped=dropped,
                  contracts={rid: f"{d.query.method} {d.query.path}"
                             for rid, d in discoveries.items()})
        if not mandatory_ok and not optional_ok:
            for rec in records.values():
                jobs.add_analyzer_run(job_id, rec)
            jobs.finish(job_id, "error", error="no analyzers reachable")
            return "⚠️ No analyzers could be reached to handle this request."

        # 4) AI CONTROLLER — ALWAYS runs. Picks the optional analyzers AND plans every call from
        #    the discovery documents fetched a moment ago (see orchestrator/decide.py).
        jobs.emit(job_id, "ai_controller", "running", "reading live analyzer contracts")
        # Decoded once per job: an analyzer with a JSON contract takes the file as TEXT in a body
        # param ({{file_text}}) rather than as a multipart upload.
        file_text = plan_mod.decode_text(file_bytes)
        decision = await decide.decide(
            question, mandatory_ok, optional_ok, discoveries,
            routing_system_prompt=cfg.routing_system_prompt,
            filename=filename, file_bytes=len(file_bytes), file_text=file_text,
        )
        jobs.set_meta(job_id, used_ai=decision.used_ai, ai_mode=decision.mode,
                      ai_reason=decision.reason, ai_system_prompt=decision.system_prompt,
                      ai_raw=decision.ai_raw, ai_plan_source=decision.plan_source,
                      ai_elapsed_ms=decision.elapsed_ms)
        jobs.emit(job_id, "ai_controller", "ok" if decision.used_ai else "skipped",
                  decision.reason, elapsed_ms=decision.elapsed_ms,
                  used_ai=decision.used_ai, mode=decision.mode,
                  selected=decision.selected_ids, plan_source=decision.plan_source,
                  system_prompt=decision.system_prompt, ai_raw=decision.ai_raw,
                  plans={k: v.model_dump(mode="json") for k, v in decision.plans.items()})

        # 5) CALL selected analyzers — ASYNC, in parallel, each with its own planned request.
        selected_refs = [r for r in refs if r.id in decision.selected_ids and r.id in discoveries]
        for rid, rec in records.items():
            rec.selected = rid in decision.selected_ids
            p = decision.plans.get(rid)
            if p is not None:
                rec.plan = p
        vlog.log(f"  ▶ calling {len(selected_refs)} analyzer(s) in parallel: "
                 f"{[r.id for r in selected_refs]}")
        results = await asyncio.gather(*[
            _call_one(job_id, r, discoveries[r.id],
                      decision.plans.get(r.id)
                      or plan_mod.deterministic(r, discoveries[r.id], question, filename,
                                                file_text),
                      file_bytes, filename, question, records[r.id])
            for r in selected_refs
        ])
        # analyzers that never ran (not discovered / not selected) are recorded too, so history
        # shows the whole decision, not just the winners.
        for rid, rec in records.items():
            if not rec.called:
                jobs.add_analyzer_run(job_id, rec)

        # 6) CONCATENATE the analyzer reports.
        jobs.emit(job_id, "concat", "running")
        sections = [r.report_markdown for r in results if r and r.report_markdown.strip()]
        combined = "\n\n---\n\n".join(sections) if sections else \
            "⚠️ No analyzer produced a result for this request."
        jobs.emit(job_id, "concat", "ok",
                  f"{len(sections)} section(s), {len(combined):,} chars",
                  sections=len(sections), chars=len(combined))

        # 7) ORB — static config; runs once at the end if enabled.
        if cfg.orb_enabled:
            jobs.emit(job_id, "orb", "running", f"asking {orb.ORB_ASK_URL}")
            t_orb = time.time()
            orb_answer = await orb.fetch(combined, question)
            orb_ms = int((time.time() - t_orb) * 1000)
            if orb_answer:
                combined += f"\n\n{orb.ORB_SECTION_HEADING}\n\n{orb_answer}"
                jobs.set_meta(job_id, orb_status="ok", orb_chars=len(orb_answer),
                              orb_elapsed_ms=orb_ms)
                jobs.emit(job_id, "orb", "ok", f"{len(orb_answer):,} chars appended",
                          elapsed_ms=orb_ms, chars=len(orb_answer))
            else:
                jobs.set_meta(job_id, orb_status="skipped", orb_elapsed_ms=orb_ms)
                jobs.emit(job_id, "orb", "skipped", "no ORB output (fail-open)",
                          elapsed_ms=orb_ms)
        else:
            jobs.set_meta(job_id, orb_status="off")
            jobs.emit(job_id, "orb", "skipped", "ORB disabled in config")

        # 8) DONE.
        jobs.emit(job_id, "done", "ok", f"{len(combined):,} chars", chars=len(combined))
        jobs.finish(job_id, "done", report_chars=len(combined),
                    selected=decision.selected_ids, orb_enabled=cfg.orb_enabled,
                    report_markdown=combined)
        return combined

    except Exception as e:  # noqa: BLE001 — never leak a raw stack to the MCP client
        vlog.log(f"✖ pipeline crashed: {type(e).__name__}: {e}", vlog.ERROR)
        jobs.emit(job_id, "done", "error", f"{type(e).__name__}: {e}")
        jobs.finish(job_id, "error", error=f"{type(e).__name__}: {e}")
        return "⚠️ The analysis pipeline encountered an internal error."
