"""CALL PLANNING — turn a live discovery document into an executable request.

This module is the safety rail around the dynamic half of the AI Controller.

Every analyzer publishes *how to call it* in its ``GET /discover`` (:mod:`orchestrator.models`
``AnalyzerQuery``): the method, the fully-qualified path, the content type, and the declared
params (name / required / type / location). When an analyzer **changes** that contract — renames
``question`` to ``query``, adds a required ``mode`` field, moves its endpoint — nothing in
VISTA-MCP needs to change: the AI Controller reads the *new* contract on the next call and plans
the request accordingly.

Two builders produce a :class:`~orchestrator.models.CallPlan`:

* :func:`deterministic` — built purely from the discovery document. This is the fallback and the
  regression floor: for the standard ``file`` + ``question`` contract it produces byte-for-byte
  the request VISTA-MCP has always sent.
* :func:`from_ai` — the AI Controller's plan, **validated field-by-field against the same
  discovery**. Anything the discovery doesn't advertise is corrected or dropped (and recorded in
  ``notes``), so a hallucinated endpoint, verb or param can never reach an analyzer.

The executor (:mod:`orchestrator.analyzer_client`) only ever runs a validated plan.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .models import AnalyzerDiscovery, AnalyzerParam, AnalyzerRef, CallPlan

# Placeholders the AI Controller may use in field values instead of echoing content back to us.
# Keeping the *content* out of the model's answer means a long question — or a whole config file
# — can never be truncated, reworded or quoted wrong on the way to an analyzer.
PH_QUESTION = "{{question}}"
PH_FILENAME = "{{filename}}"
# The uploaded file's TEXT, for an analyzer that takes content in a body/form param rather than
# as a multipart upload. ORB's config-validate is exactly this: a JSON contract whose
# `config_snippet` is the config itself. Without this placeholder a JSON-contract analyzer could
# never receive the file at all.
PH_FILE_TEXT = "{{file_text}}"

# Name heuristics used ONLY by the deterministic builder (the AI Controller reads the real
# param descriptions instead of guessing from names).
QUESTION_ALIASES = {"question", "query", "prompt", "ask", "user_question", "q", "text"}
FILENAME_ALIASES = {"filename", "file_name", "name", "log_name"}
# Checked BEFORE the question aliases: a param named for the file's content wants the content.
FILE_TEXT_ALIASES = {"config_snippet", "config_text", "file_text", "file_content", "content",
                     "snippet", "config", "log_text"}

MAX_FIELD_CHARS = 20000
# A config or log pasted into a body param is legitimately large; capping it at MAX_FIELD_CHARS
# would silently validate only the first page of a real FortiGate config.
MAX_FILE_TEXT_CHARS = int(__import__("os").getenv("MAX_FILE_TEXT_CHARS", "400000"))


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _file_params(disc: AnalyzerDiscovery) -> list[str]:
    return [p.name for p in disc.query.params if p.location == "file" or p.type == "file"]


def _form_params(disc: AnalyzerDiscovery) -> list:
    return [p for p in disc.query.params if not (p.location == "file" or p.type == "file")]


def _resolve(value: str, question: str, filename: str, file_text: str = "") -> str:
    """Substitute the placeholders the AI Controller is told to use."""
    out = str(value if value is not None else "")
    had_file = PH_FILE_TEXT in out
    if PH_QUESTION in out:
        out = out.replace(PH_QUESTION, question or "")
    if PH_FILENAME in out:
        out = out.replace(PH_FILENAME, filename or "")
    if had_file:
        out = out.replace(PH_FILE_TEXT, file_text or "")
    return out[:MAX_FILE_TEXT_CHARS if had_file else MAX_FIELD_CHARS]


def _typed(param: AnalyzerParam | None, resolved: str) -> Any:
    """Cast a resolved string to the param's DECLARED type (see AnalyzerParam.coerce)."""
    return param.coerce(resolved) if param is not None else resolved


def decode_text(file_bytes: bytes) -> str:
    """The uploaded file as text, for {{file_text}}. Lossy rather than fatal on odd encodings."""
    if not file_bytes:
        return ""
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("utf-8", errors="replace")


def describe_contract(ref: AnalyzerRef, disc: AnalyzerDiscovery) -> str:
    """Render one analyzer's live contract for the AI Controller prompt.

    This is what makes the decision *discovery-aware*: the model is shown the analyzer's real,
    current request shape on every run — not a shape baked into this repo.
    """
    q = disc.query
    lines = [
        f'- id="{ref.id}"  ({disc.analyzer.title or ref.id})'
        f'  [{"MANDATORY" if ref.mandatory else "OPTIONAL"}]',
        f"    when_to_use: {disc.analyzer.when_to_use or '(not stated)'}",
    ]
    if disc.analyzer.summary:
        lines.append(f"    summary: {disc.analyzer.summary}")
    if disc.analyzer.supported_log_types:
        lines.append(f"    supported_log_types: {', '.join(disc.analyzer.supported_log_types)}")
    lines.append(f"    call: {q.method} {q.path}   content_type={q.content_type}")
    if q.params:
        for p in q.params:
            req = "required" if p.required else "optional"
            dft = f", default={p.default!r}" if p.default not in (None, "") else ""
            desc = f" — {p.description}" if p.description else ""
            lines.append(f"      · param \"{p.name}\" ({req}, type={p.type}, "
                         f"location={p.location}{dft}){desc}")
    else:
        lines.append("      · (no params declared — send the file as \"file\")")
    return "\n".join(lines)


def deterministic(ref: AnalyzerRef, disc: AnalyzerDiscovery, question: str,
                  filename: str, file_text: str = "") -> CallPlan:
    """Build the request straight from the discovery document — no AI involved.

    This is the fallback whenever the AI Controller is off, unreachable, or produced something
    unusable, and it is also the reference every AI plan is validated against.

    For the standard ``file`` + ``question`` contract this produces byte-for-byte the request
    VISTA-MCP has always sent — that regression floor is asserted in ``tests/``.
    """
    q = disc.query
    files = _file_params(disc)
    fields: dict[str, Any] = {}
    notes: list[str] = []

    for p in _form_params(disc):
        low = p.name.lower()
        if low in FILE_TEXT_ALIASES and file_text:
            # A body/form param named for the file's content, on a contract that declares no
            # file upload — send the content. Without this an analyzer like ORB's
            # config-validate would be called with an empty snippet.
            fields[p.name] = file_text[:MAX_FILE_TEXT_CHARS]
        elif low in QUESTION_ALIASES:
            if question:
                fields[p.name] = question[:MAX_FIELD_CHARS]
            elif p.required:
                fields[p.name] = p.default
        elif low in FILENAME_ALIASES:
            fields[p.name] = filename
        elif p.default not in (None, ""):
            # The analyzer advertised a default — the correct value to send without an AI.
            # (`not in (None, "")` rather than truthiness: `false` and `0` are real defaults.)
            fields[p.name] = p.default
        elif p.required:
            # A required field we have no value for and the analyzer suggested none. Send it
            # empty rather than omit it, and say so — this is exactly the case the AI Controller
            # exists to handle (it reads the param's description and picks a value).
            fields[p.name] = ""
            notes.append(f'required param "{p.name}" has no default and no known value '
                         f'— sent empty (the AI Controller is the intended source for this)')
        else:
            continue
        fields[p.name] = _typed(p, fields[p.name])

    return CallPlan(
        analyzer_id=ref.id,
        method=(q.method or "POST").upper(),
        url=q.path,
        content_type=q.content_type or "multipart/form-data",
        file_param=files[0] if files else ("file" if "multipart" in (q.content_type or "") else ""),
        fields=fields,
        source="deterministic",
        notes=notes,
    )


def from_ai(raw: dict, ref: AnalyzerRef, disc: AnalyzerDiscovery, question: str,
            filename: str, file_text: str = "") -> CallPlan:
    """Validate one AI-Controller invocation against the discovery and return a safe plan.

    Every field is checked against what the analyzer actually advertised. Corrections are applied
    silently but recorded in ``notes`` (surfaced on the analyzer node in the GUI and stored in
    the database), so an operator can always see the difference between what the model proposed
    and what was sent.
    """
    base = deterministic(ref, disc, question, filename, file_text)
    if not isinstance(raw, dict):
        return base

    q = disc.query
    notes: list[str] = []
    corrected = False

    # --- method: the discovery is authoritative ---
    method = str(raw.get("method") or "").strip().upper()
    want_method = (q.method or "POST").upper()
    if method and method != want_method:
        notes.append(f"method '{method}' → '{want_method}' (as discovered)")
        corrected = True
    method = want_method

    # --- url: must be exactly the discovered query path ---
    url_ai = _norm_url(str(raw.get("url") or raw.get("path") or ""))
    if url_ai and url_ai != _norm_url(q.path):
        if _origin(url_ai) == _origin(q.path):
            notes.append(f"path '{urlsplit(url_ai).path}' → the discovered path")
        else:
            notes.append(f"url host '{_origin(url_ai) or '?'}' rejected → the discovered URL")
        corrected = True
    url = q.path

    # --- content type ---
    ctype_ai = str(raw.get("content_type") or "").strip()
    ctype = q.content_type or "multipart/form-data"
    if ctype_ai and ctype_ai.split(";")[0].strip() != ctype.split(";")[0].strip():
        notes.append(f"content_type '{ctype_ai}' → '{ctype}' (as discovered)")
        corrected = True

    # --- the file param ---
    declared_files = _file_params(disc)
    file_param = str(raw.get("file_param") or "").strip()
    if declared_files:
        if file_param not in declared_files:
            if file_param:
                notes.append(f'file_param "{file_param}" is not declared → "{declared_files[0]}"')
                corrected = True
            file_param = declared_files[0]
    else:
        # The analyzer declared no file param. For a multipart contract the file still has to go
        # somewhere, so fall back to the same historic default the deterministic builder uses
        # ("file"). For a JSON contract there is nothing to attach as a multipart part — the file
        # travels as text in a body field via {{file_text}}, which is a plan the AI is told to
        # make and which needs no file_param at all.
        if file_param and file_param != base.file_param:
            notes.append(f'file_param "{file_param}" is not declared → "{base.file_param or "none"}"')
            corrected = True
        file_param = base.file_param

    # --- form fields: only declared params survive ---
    declared_form = {p.name: p for p in _form_params(disc)}
    raw_fields = raw.get("fields")
    fields: dict[str, str] = {}
    if isinstance(raw_fields, dict):
        for name, value in raw_fields.items():
            name = str(name)
            if name in declared_files:
                continue                              # the file is attached, not a form value
            # An analyzer that declares nothing accepts nothing: sending an invented field to it
            # is exactly the class of call this validation exists to prevent.
            if name not in declared_form:
                notes.append(f'field "{name}" is not declared by the analyzer → dropped')
                corrected = True
                continue
            param = declared_form.get(name)
            resolved = _resolve(value if isinstance(value, str) else str(value),
                                question, filename, file_text)
            # Emptiness is judged on the STRING, before casting: a boolean param whose value is
            # "false" casts to False, which is falsy but entirely meaningful.
            if not resolved and param is not None and not param.required:
                continue                              # optional + empty ⇒ omit (as we always have)
            fields[name] = _typed(param, resolved)
    elif raw_fields is not None:
        notes.append("fields was not an object → rebuilt from the discovery")
        corrected = True
        fields = dict(base.fields)
    else:
        fields = dict(base.fields)

    # --- every required form param must be present ---
    for name, param in declared_form.items():
        if param.required and name not in fields:
            fields[name] = base.fields.get(name, "")
            notes.append(f'required param "{name}" was missing → filled from the discovery')
            corrected = True

    return CallPlan(
        analyzer_id=ref.id,
        method=method,
        url=url,
        content_type=ctype,
        file_param=file_param,
        fields=fields,
        source="ai-corrected" if corrected else "ai",
        notes=notes,
        ai_note=str(raw.get("note") or "")[:500],
    )


def summarize(plan: CallPlan) -> str:
    """One-line human summary of a plan, for CLI logs and node details."""
    bits = [f"{plan.method} {plan.url}"]
    if plan.file_param:
        bits.append(f'file→"{plan.file_param}"')
    if plan.fields:
        bits.append("fields=" + ",".join(sorted(plan.fields)))
    bits.append(f"[{plan.source}]")
    return "  ".join(bits)
