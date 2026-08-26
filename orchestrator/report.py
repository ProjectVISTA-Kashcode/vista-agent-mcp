"""REPORT NORMALISATION — turn any analyzer's result into the markdown section we concatenate.

The standard contract says an analyzer returns ``report_markdown``. Real analyzers don't always:
ORB's ``config-extract`` / ``config-validate`` return

    {"analyzer": "...", "ok": true, "reasoning": "...", "result": {...}, "meta": {...}}

— no markdown anywhere. Before this module those results validated fine and produced an **empty**
section, so a job whose only analyzer was ORB reported "No analyzer produced a result".

Three tiers, in order, first one that yields text wins:

1. **native** — ``report_markdown`` is present. Returned untouched. This is the LogV path and it
   is bit-for-bit what it has always been; nothing below can affect it.
2. **rendered** — a deterministic renderer walks the result and writes the markdown itself. It
   knows the shapes VISTA analyzers actually emit (``reasoning``, ``snippets``, ``counts``,
   ``flagged``, ``structural``) and falls back to a generic recursive walk. **No AI involved**, so
   a new analyzer works with the controller switched off.
3. **ai** — :func:`orchestrator.ai_controller.render_report` on the *long* model. Used only when
   tier 2 comes out thin against a payload that clearly carries more (``AI_REPORT_RENDER=auto``,
   the default), or always (``=1``), or never (``=0``).

Env::

    AI_REPORT_RENDER   auto (default) | 1 | 0
    AI_RENDER_MIN_CHARS   below this, an auto-mode render is considered thin (default 240)
"""
from __future__ import annotations

import json
import os
from typing import Any

import vlog

MODE = os.getenv("AI_REPORT_RENDER", "auto").strip().lower()
MIN_CHARS = int(os.getenv("AI_RENDER_MIN_CHARS", "240"))

# meta keys that are plumbing, not findings — kept out of the report body.
_NOISY_META = {"secs", "session_id", "ran", "total_chunks", "index_status"}

_MAX_SNIPPET_CHARS = 12000
_MAX_LIST_ITEMS = 40


def _fence(text: str, lang: str = "text") -> str:
    body = str(text).rstrip()
    if len(body) > _MAX_SNIPPET_CHARS:
        body = body[:_MAX_SNIPPET_CHARS] + f"\n… (+{len(str(text)) - _MAX_SNIPPET_CHARS:,} chars)"
    fence = "```"
    while fence in body:                      # a config containing ``` would break the block
        fence += "`"
    return f"{fence}{lang}\n{body}\n{fence}"


def _humanize(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _render_snippets(snippets: list) -> list[str]:
    """ORB config-extract: verbatim CLI sections lifted from the uploaded config."""
    out: list[str] = []
    for i, sn in enumerate(snippets[:_MAX_LIST_ITEMS], 1):
        if not isinstance(sn, dict):
            out.append(_fence(sn))
            continue
        text = sn.get("text") or sn.get("snippet") or ""
        if not str(text).strip():
            continue
        path = sn.get("config_path") or sn.get("path") or ""
        score = sn.get("score")
        head = f"**{path}**" if path else f"**Section {i}**"
        if score is not None:
            head += f"  ·  relevance {_scalar(score)}"
        out.append(f"{head}\n\n{_fence(text)}")
    if len(snippets) > _MAX_LIST_ITEMS:
        out.append(f"_… and {len(snippets) - _MAX_LIST_ITEMS} more section(s)._")
    return out


def _render_mapping(name: str, mapping: dict) -> str:
    """A flat dict (counts, structural, …) as a bullet list."""
    rows = [f"- **{_humanize(k)}:** {_scalar(v)}"
            for k, v in mapping.items() if not isinstance(v, (dict, list))]
    return f"**{_humanize(name)}**\n" + "\n".join(rows) if rows else ""


def _render_list(name: str, items: list) -> str:
    rows = []
    for it in items[:_MAX_LIST_ITEMS]:
        if isinstance(it, dict):
            inline = ", ".join(f"{_humanize(k)}: {_scalar(v)}" for k, v in it.items()
                               if not isinstance(v, (dict, list)))
            rows.append(f"- {inline or json.dumps(it)[:300]}")
        else:
            rows.append(f"- {_scalar(it)}")
    if len(items) > _MAX_LIST_ITEMS:
        rows.append(f"- _… and {len(items) - _MAX_LIST_ITEMS} more._")
    return f"**{_humanize(name)}** ({len(items)})\n" + "\n".join(rows)


def _render_result(result: Any) -> list[str]:
    """Walk an analyzer's ``result`` object into markdown blocks."""
    if result in (None, "", {}, []):
        return []
    if isinstance(result, str):
        return [result.strip()]
    if isinstance(result, list):
        return [_render_list("results", result)]
    if not isinstance(result, dict):
        return [_scalar(result)]

    blocks: list[str] = []
    # Known shapes first, so the common analyzers read well.
    if isinstance(result.get("snippets"), list) and result["snippets"]:
        blocks += _render_snippets(result["snippets"])
    if str(result.get("note") or "").strip():
        blocks.append(str(result["note"]).strip())

    for key, value in result.items():
        if key in ("snippets", "note"):
            continue
        if isinstance(value, dict):
            block = _render_mapping(key, value)
            if block:
                blocks.append(block)
        elif isinstance(value, list):
            if value:
                blocks.append(_render_list(key, value))
        elif key != "ok":                     # `ok` is already carried by the status line
            blocks.append(f"- **{_humanize(key)}:** {_scalar(value)}")
    return [b for b in blocks if b.strip()]


def render(payload: dict, *, analyzer_id: str = "", title: str = "") -> str:
    """Deterministically render a non-markdown analyzer payload. Never raises, may return ''."""
    try:
        heading = title or payload.get("title") or payload.get("analyzer") or analyzer_id or "Analyzer"
        parts: list[str] = [f"## {heading}"]

        err = payload.get("error")
        if err:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code", "") if isinstance(err, dict) else ""
            parts.append(f"⚠️ {msg}" + (f" _(code: {code})_" if code else ""))

        reasoning = str(payload.get("reasoning") or payload.get("summary") or "").strip()
        if reasoning:
            parts.append(reasoning)

        parts += _render_result(payload.get("result"))

        meta = payload.get("meta")
        if isinstance(meta, dict):
            bits = [f"{_humanize(k)}: {_scalar(v)}" for k, v in meta.items()
                    if k not in _NOISY_META and not isinstance(v, (dict, list))
                    and v not in (None, "", [])]
            if bits:
                parts.append("_" + "  ·  ".join(bits) + "_")

        body = "\n\n".join(p for p in parts[1:] if str(p).strip())
        return f"{parts[0]}\n\n{body}".strip() if body else ""
    except Exception as e:  # noqa: BLE001 — rendering must never break a job
        vlog.log(f"  ✖ report render failed ({type(e).__name__}: {e})", vlog.WARNING)
        return ""


def _looks_thin(rendered: str, payload: dict) -> bool:
    """Did the deterministic renderer under-serve a payload that clearly carries more?"""
    if not rendered.strip():
        return True
    try:
        raw_size = len(json.dumps(payload.get("result") or {}))
    except Exception:  # noqa: BLE001
        raw_size = 0
    return len(rendered) < MIN_CHARS and raw_size > MIN_CHARS * 2


async def normalize(payload: dict, *, analyzer_id: str = "", title: str = "",
                    question: str = "") -> tuple[str, str]:
    """Best markdown for one analyzer payload. Returns ``(markdown, source)``.

    ``source`` is ``native`` | ``rendered`` | ``ai`` | ``empty`` — recorded on the job so the
    console shows where a section's text actually came from.
    """
    native = str(payload.get("report_markdown") or "").strip()
    if native:
        return native, "native"                     # the LogV path — untouched, always wins

    rendered = render(payload, analyzer_id=analyzer_id, title=title)

    want_ai = MODE == "1" or (MODE == "auto" and _looks_thin(rendered, payload))
    if want_ai:
        from . import ai_controller                 # local import: keeps report.py AI-optional
        prompt = (
            f"ANALYZER: {title or analyzer_id or payload.get('analyzer') or 'analyzer'}\n"
            f"USER QUESTION: \"{question or '(none)'}\"\n\n"
            f"RAW RESULT JSON:\n{json.dumps(payload, indent=2, default=str)[:60000]}"
        )
        md, _ms, _note = await ai_controller.render_report(prompt)
        if md and md.strip():
            return md.strip(), "ai"

    return (rendered, "rendered") if rendered.strip() else ("", "empty")
