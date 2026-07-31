"""DISCOVER TOOLS — query each configured analyzer's ``/discover`` endpoint.

Because every analyzer implements the same standard discovery shape (``docs/analyzer_api.md``),
this is ONE generic function. It returns the parsed :class:`~orchestrator.models.AnalyzerDiscovery`
(what the analyzer does + how to call it) for each analyzer, fetched concurrently.

Fail-soft: if an analyzer's discovery is unreachable/invalid we log it and drop that analyzer
from the run rather than failing the whole request.
"""
from __future__ import annotations

import asyncio
import time

import httpx

import tlsconf
import vlog

from .models import AnalyzerDiscovery, AnalyzerRef


async def _fetch_one(ref: AnalyzerRef) -> AnalyzerDiscovery | None:
    url = ref.resolved_discover_url()
    t0 = time.time()
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.json()
        # tolerate an ApiResponse envelope ({ok,kind,data}) OR a raw discovery document
        if isinstance(raw, dict) and "analyzer" not in raw and isinstance(raw.get("data"), dict):
            raw = raw["data"]
        disc = AnalyzerDiscovery.model_validate(raw)
        vlog.log(f"  ✔ discover[{ref.id}] '{disc.analyzer.title}' "
                 f"→ query {disc.query.method} {disc.query.path}  ({int((time.time()-t0)*1000)}ms)")
        return disc
    except Exception as e:  # noqa: BLE001 — fail-soft per analyzer
        vlog.log(f"  ✖ discover[{ref.id}] FAILED ({type(e).__name__}: {e}) → dropping this analyzer",
                 vlog.WARNING)
        return None


async def probe(url: str) -> tuple[AnalyzerDiscovery | None, str]:
    """Fetch ONE discovery document and report the failure reason if it doesn't answer.

    Used by the GUI's "Add tool / Add analyzer" flow so an operator can paste a base URL and see
    immediately whether the analyzer speaks the standard contract — and auto-fill its id, title
    and ``when_to_use`` from what it actually advertises.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None, "URL must start with http:// or https://"
    if not url.rstrip("/").endswith("/discover"):
        url = url.rstrip("/") + "/discover"
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.json()
        if isinstance(raw, dict) and "analyzer" not in raw and isinstance(raw.get("data"), dict):
            raw = raw["data"]
        return AnalyzerDiscovery.model_validate(raw), ""
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code} from {url}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def discover(refs: list[AnalyzerRef]) -> dict[str, AnalyzerDiscovery]:
    """Fetch discovery for every ref concurrently. Returns {ref.id: AnalyzerDiscovery} for
    the ones that answered."""
    if not refs:
        return {}
    results = await asyncio.gather(*[_fetch_one(r) for r in refs])
    return {ref.id: disc for ref, disc in zip(refs, results) if disc is not None}
