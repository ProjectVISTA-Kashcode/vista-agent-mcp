"""DISCOVER TOOLS — query each configured analyzer's ``/discover`` endpoint.

Every analyzer implements the same standard discovery shape (``docs/analyzer_api.md``), so this
is ONE generic function for all of them, fetched concurrently.

A discovery URL may answer with either of **two** documents:

* a **single** analyzer — ``{"analyzer": {...}, "query": {...}}`` (LogV, the fakes), or
* a **catalog** — ``{"schema_version": "1.0", "analyzers": [ {...}, {...} ]}``.

ORB does the latter: ``https://vista.fortinet.com/orb/discover`` advertises ``config-extract``
and ``config-validate`` together, and the individual routes have **no** ``/discover`` of their own
(they 404). One config entry can therefore either pick one analyzer out of a catalog
(``catalog_select``) or expand into all of them — see :meth:`AnalyzerRef.child`.

Because a catalog entry can become several runtime analyzers, :func:`discover` returns the
**runtime** ref list alongside the discoveries; the rest of the pipeline works off that.

Fail-soft: an unreachable or invalid discovery drops that analyzer from the run and logs why,
rather than failing the whole request.
"""
from __future__ import annotations

import asyncio
import time

import httpx

import tlsconf
import vlog

from .models import AnalyzerCatalog, AnalyzerDiscovery, AnalyzerRef


def _unwrap(raw):
    """Tolerate an ``ApiResponse`` envelope (``{ok, kind, data}``) around either document."""
    if (isinstance(raw, dict) and "analyzer" not in raw and "analyzers" not in raw
            and isinstance(raw.get("data"), dict)):
        return raw["data"]
    return raw


def is_catalog(raw) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("analyzers"), list) and "analyzer" not in raw


def parse(raw) -> AnalyzerDiscovery | AnalyzerCatalog:
    """Parse either discovery document. Raises if it is neither."""
    raw = _unwrap(raw)
    return AnalyzerCatalog.model_validate(raw) if is_catalog(raw) else AnalyzerDiscovery.model_validate(raw)


def _select(ref: AnalyzerRef, cat: AnalyzerCatalog) -> list[tuple[AnalyzerRef, AnalyzerDiscovery]]:
    """Resolve one config entry against a catalog: pick one analyzer, or expand into all."""
    by_id = {d.analyzer.id: d for d in cat.analyzers if d.analyzer.id}
    if not by_id:
        vlog.log(f"  ✖ discover[{ref.id}] catalog advertised no analyzers → dropping", vlog.WARNING)
        return []

    want = (ref.catalog_select or "").strip()
    if want:
        disc = by_id.get(want)
        if disc is None:
            vlog.log(f"  ✖ discover[{ref.id}] catalog has no analyzer '{want}' "
                     f"(offers: {', '.join(sorted(by_id))}) → dropping", vlog.WARNING)
            return []
        vlog.log(f"  ✔ discover[{ref.id}] catalog → selected '{want}' "
                 f"({disc.analyzer.title}) · {disc.query.method} {disc.query.path}")
        return [(ref, disc)]

    if ref.id in by_id:                       # the entry names a catalog member directly
        disc = by_id[ref.id]
        vlog.log(f"  ✔ discover[{ref.id}] catalog → matched by id · "
                 f"{disc.query.method} {disc.query.path}")
        return [(ref, disc)]

    # No selector: expand every member into its own runtime analyzer.
    out = []
    for sub_id, disc in by_id.items():
        child = ref.child(f"{ref.id}:{sub_id}", disc)
        out.append((child, disc))
    vlog.log(f"  ✔ discover[{ref.id}] catalog → expanded {len(out)} analyzer(s): "
             f"{', '.join(r.id for r, _ in out)}")
    return out


async def _fetch_one(ref: AnalyzerRef) -> list[tuple[AnalyzerRef, AnalyzerDiscovery]]:
    """Discover one config entry. Returns 0, 1 or N (catalog) runtime (ref, discovery) pairs."""
    url = ref.resolved_discover_url()
    t0 = time.time()
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        doc = parse(resp.json())
    except Exception as e:  # noqa: BLE001 — fail-soft per analyzer
        vlog.log(f"  ✖ discover[{ref.id}] FAILED ({type(e).__name__}: {vlog.short(e, 200)}) "
                 f"→ dropping this analyzer", vlog.WARNING)
        return []

    ms = int((time.time() - t0) * 1000)
    if isinstance(doc, AnalyzerCatalog):
        vlog.log(f"  · discover[{ref.id}] catalog with {len(doc.analyzers)} analyzer(s) ({ms}ms)")
        return _select(ref, doc)

    vlog.log(f"  ✔ discover[{ref.id}] '{doc.analyzer.title}' "
             f"→ query {doc.query.method} {doc.query.path}  ({ms}ms)")
    return [(ref, doc)]


async def probe(url: str) -> tuple[AnalyzerDiscovery | AnalyzerCatalog | None, str]:
    """Fetch ONE discovery document and report the failure reason if it doesn't answer.

    Used by the console's "Add tool / Add analyzer" flow so an operator can paste a base URL and
    see immediately whether the analyzer speaks the standard contract — and auto-fill its id,
    title and ``when_to_use`` from what it actually advertises. Returns a catalog unchanged, so
    the editor can offer every route it advertises.
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
        return parse(resp.json()), ""
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code} from {url}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def discover(
    refs: list[AnalyzerRef],
) -> tuple[dict[str, AnalyzerDiscovery], list[AnalyzerRef], list[str]]:
    """Discover every config entry concurrently.

    Returns ``(discoveries, runtime_refs, dropped_config_ids)`` where ``discoveries`` is keyed by
    **runtime** analyzer id and ``runtime_refs`` is the expanded, config-ordered list the rest of
    the pipeline runs on. For a non-catalog analyzer the runtime ref *is* the config ref, so this
    is a no-op for every analyzer that existed before catalogs.
    """
    if not refs:
        return {}, [], []

    results = await asyncio.gather(*[_fetch_one(r) for r in refs])
    discoveries: dict[str, AnalyzerDiscovery] = {}
    runtime: list[AnalyzerRef] = []
    dropped: list[str] = []
    for ref, pairs in zip(refs, results):
        if not pairs:
            dropped.append(ref.id)
            continue
        for rt_ref, disc in pairs:
            if rt_ref.id in discoveries:      # two entries resolving to the same id — keep first
                vlog.log(f"  ⚠ discover: duplicate analyzer id '{rt_ref.id}' → keeping the first",
                         vlog.WARNING)
                continue
            discoveries[rt_ref.id] = disc
            runtime.append(rt_ref)
    return discoveries, runtime, dropped
