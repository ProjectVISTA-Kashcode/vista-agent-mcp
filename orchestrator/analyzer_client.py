"""The ONE generic function that calls ANY analyzer.

Because every analyzer advertises the same query contract in its ``/discover`` (multipart
``file`` + optional ``question`` → :class:`~orchestrator.models.AnalyzerResult`), a single
function handles them all — no per-analyzer code. Add an analyzer to a tool's config and this
function calls it, using the query path and param names straight from its discovery document.

Returns a normalised :class:`~orchestrator.models.AnalyzerResult` (``ok=False`` with an error
report on any failure — never raises), so the orchestrator can always concatenate a section.
"""
from __future__ import annotations

import time

import httpx

import tlsconf
import vlog

from .models import AnalyzerDiscovery, AnalyzerRef, AnalyzerResult


def _err_result(ref: AnalyzerRef, code: str, message: str) -> AnalyzerResult:
    return AnalyzerResult(
        ok=False, analyzer_id=ref.id, title=ref.title or ref.id,
        report_markdown=f"## {ref.title or ref.id}\n\n⚠️ {message}",
        error={"code": code, "message": message},
    )


async def call(
    ref: AnalyzerRef,
    disc: AnalyzerDiscovery,
    file_bytes: bytes,
    filename: str,
    question: str,
) -> AnalyzerResult:
    """Call one analyzer's query endpoint and return its standard result."""
    q = disc.query
    url = q.path
    # Build the multipart body from the DECLARED params (generic across analyzers).
    files: dict = {}
    data: dict = {}
    for p in q.params:
        if p.location == "file":
            files[p.name] = (filename or "logfile.log", file_bytes, "application/octet-stream")
        elif p.name == "question" and question:
            data[p.name] = question
    if not files:  # discovery didn't declare a file param → default to `file`
        files["file"] = (filename or "logfile.log", file_bytes, "application/octet-stream")

    vlog.log(f"  → call[{ref.id}] {q.method} {url}  (file={len(file_bytes):,}B, "
             f"question={'yes' if question else 'no'})")
    t0 = time.time()
    timeout = httpx.Timeout(connect=5.0, read=ref.timeout, write=30.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
            resp = await client.request(q.method, url, files=files, data=data)
    except Exception as e:  # noqa: BLE001
        cert = tlsconf.is_cert_error(e)
        vlog.log(f"  ✖ call[{ref.id}] unreachable ({type(e).__name__})"
                 + (" — TLS cert rejected" if cert else ""), vlog.ERROR)
        return _err_result(ref, "unreachable",
                           f"Could not reach the {ref.title or ref.id} analyzer "
                           + ("(its TLS certificate was rejected)." if cert else f"({type(e).__name__})."))

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
                           f"The {ref.title or ref.id} analyzer returned HTTP {resp.status_code}.")

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        vlog.log(f"  ✖ call[{ref.id}] non-JSON response", vlog.WARNING)
        return _err_result(ref, "bad_response",
                           f"The {ref.title or ref.id} analyzer returned a non-JSON response.")

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
    return result
