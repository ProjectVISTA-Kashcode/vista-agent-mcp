"""The ONE generic function that calls ANY analyzer — by executing a validated call plan.

Because every analyzer advertises its query contract in its ``/discover`` (:mod:`orchestrator.
plan` turns that contract, plus the AI Controller's decision, into a
:class:`~orchestrator.models.CallPlan`), a single function handles them all — no per-analyzer
code. Add an analyzer to a tool's config and this function calls it; change an analyzer's API
and the *next* call follows the new contract, because the plan is rebuilt from the live
discovery every time.

This module only **executes** a plan — it never invents one. Whatever the plan says was already
checked against the discovery, so an unknown endpoint or param can't get here.

Returns a normalised :class:`~orchestrator.models.AnalyzerResult` (``ok=False`` with an error
report on any failure — never raises), so the orchestrator can always concatenate a section.
"""
from __future__ import annotations

import time

import httpx

import tlsconf
import vlog

from .models import AnalyzerDiscovery, AnalyzerRef, AnalyzerResult, CallPlan
from .plan import deterministic as build_plan


def _err_result(ref: AnalyzerRef, code: str, message: str) -> AnalyzerResult:
    return AnalyzerResult(
        ok=False, analyzer_id=ref.id, title=ref.title or ref.id,
        report_markdown=f"## {ref.title or ref.id}\n\n⚠️ {message}",
        error={"code": code, "message": message},
    )


def _file_targets(plan: CallPlan, disc: AnalyzerDiscovery) -> list[str]:
    """Param names the file is attached to: the planned one plus any REQUIRED declared file param."""
    names: list[str] = []
    if plan.file_param:
        names.append(plan.file_param)
    for p in disc.query.params:
        if (p.location == "file" or p.type == "file") and p.required and p.name not in names:
            names.append(p.name)
    return names


async def call(
    ref: AnalyzerRef,
    disc: AnalyzerDiscovery,
    file_bytes: bytes,
    filename: str,
    question: str,
    plan: CallPlan | None = None,
) -> tuple[AnalyzerResult, int | None]:
    """Execute one analyzer's call plan. Returns ``(result, http_status)``; never raises."""
    # No plan (defensive — the pipeline always passes one): build it from the discovery.
    if plan is None:
        plan = build_plan(ref, disc, question, filename)

    url = plan.url or disc.query.path
    is_json = "json" in (plan.content_type or "").lower()

    files: dict = {}
    data: dict = {}
    json_body: dict | None = None
    if is_json:
        json_body = dict(plan.fields)
    else:
        for name in _file_targets(plan, disc):
            files[name] = (filename or "logfile.log", file_bytes, "application/octet-stream")
        data = dict(plan.fields)
        if not files and not any(
            (p.location == "file" or p.type == "file") for p in disc.query.params
        ):
            # multipart with no declared file param at all — keep the historical default so an
            # analyzer that documents nothing still receives the log.
            files["file"] = (filename or "logfile.log", file_bytes, "application/octet-stream")

    vlog.log(f"  → call[{ref.id}] {plan.method} {url}  (file={len(file_bytes):,}B → "
             f"{plan.file_param or 'n/a'}, fields={sorted(plan.fields) or '[]'}, "
             f"plan={plan.source})")
    for note in plan.notes:
        vlog.log(f"      ↳ plan note: {note}", vlog.WARNING)

    t0 = time.time()
    timeout = httpx.Timeout(connect=5.0, read=ref.timeout, write=30.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
            if json_body is not None:
                resp = await client.request(plan.method, url, json=json_body)
            else:
                resp = await client.request(plan.method, url, files=files, data=data)
    except Exception as e:  # noqa: BLE001
        cert = tlsconf.is_cert_error(e)
        vlog.log(f"  ✖ call[{ref.id}] unreachable ({type(e).__name__})"
                 + (" — TLS cert rejected" if cert else ""), vlog.ERROR)
        return _err_result(ref, "unreachable",
                           f"Could not reach the {ref.title or ref.id} analyzer "
                           + ("(its TLS certificate was rejected)." if cert
                              else f"({type(e).__name__}).")), None

    dt = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json() or {}).get("error", "") or resp.text[:160]
        except Exception:  # noqa: BLE001
            detail = resp.text[:160]
        vlog.log(f"  ✖ call[{ref.id}] HTTP {resp.status_code} in {dt}ms: {vlog.short(detail,120)}",
                 vlog.WARNING)
        return _err_result(ref, f"http_{resp.status_code}",
                           f"The {ref.title or ref.id} analyzer returned HTTP "
                           f"{resp.status_code}."), resp.status_code

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        vlog.log(f"  ✖ call[{ref.id}] non-JSON response", vlog.WARNING)
        return _err_result(ref, "bad_response",
                           f"The {ref.title or ref.id} analyzer returned a non-JSON "
                           f"response."), resp.status_code

    try:
        result = AnalyzerResult.model_validate(payload)
    except Exception as e:  # noqa: BLE001
        vlog.log(f"  ✖ call[{ref.id}] result did not match the standard schema ({e})", vlog.WARNING)
        # be forgiving: wrap whatever report text we can find
        text = payload.get("report_markdown") or payload.get("report") or ""
        result = AnalyzerResult(ok=True, analyzer_id=ref.id, title=ref.title or ref.id,
                                report_markdown=str(text))
    if not result.title:
        result.title = ref.title or ref.id
    if not result.analyzer_id:
        result.analyzer_id = ref.id
    vlog.log(f"  ✔ call[{ref.id}] ok in {dt}ms → {len(result.report_markdown):,}-char report "
             f"(supported={result.meta.get('supported')})")
    return result, resp.status_code
