"""AI CONTROLLER — the typed Pydantic AI agents the orchestrator thinks with.

Every AI step in VISTA-MCP is a **Pydantic AI agent with a declared output model**, run against
the Fortinet AgentAssist gateway (:mod:`orchestrator.ai_model`). Nothing here parses model text
by hand: the agent's answer arrives already validated into a Pydantic object, and Pydantic AI
re-prompts the model itself when it doesn't fit the schema.

Three agents, three jobs:

:func:`route_and_plan`  **fast**  Runs on EVERY tool call. Reads each analyzer's freshly-fetched
                                 contract and returns which optional analyzers to run *and* a
                                 planned invocation for each analyzer that will run. The plan is
                                 then validated field-by-field against that same discovery in
                                 :mod:`orchestrator.plan` — this module proposes, that one
                                 disposes. See :mod:`orchestrator.decide` for how it is used.

:func:`render_report`   **long** Turns an analyzer result that carries **no** ``report_markdown``
                                 into a readable section. Only ever a *fallback*: a deterministic
                                 renderer runs first (:mod:`orchestrator.report`), so analyzers
                                 like ORB's work with the AI switched off.

:func:`draft_tool`      **pro**  Reads a discovery document — single **or catalog** — and drafts
                                 a complete TOOL_ENABLEMENT entry: tool name, client-facing
                                 description, routing system prompt, and one analyzer entry per
                                 route. Powers the console's "build this tool with AI" button.
                                 The draft is always shown to an operator before it is saved.

**Fail-safe.** Every function returns ``None`` on any failure. The callers all have a
deterministic path, so a gateway outage changes latency and quality, never correctness.

The planned *field values* are deliberately typed ``str`` in the schema below, even for boolean
or integer params. Free-form JSON values are the part of a structured answer models get wrong
most often, and the real type is already in the discovery — so the cast happens deterministically
in :meth:`~orchestrator.models.AnalyzerParam.coerce`, not in the model's head.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

import vlog

from . import ai_model

# Re-exported so the startup banner and the console keep one import for "the AI Controller".
ENABLED = ai_model.ENABLED
TIMEOUT = ai_model.TIMEOUT
BASE_URL = ai_model.BASE_URL
GEN_URL = ai_model.BASE_URL          # legacy name used by the banner / GUI status payload
NAME = ai_model.NAME
describe = ai_model.describe
health = ai_model.health


# =========================================================================== #
# 1. ROUTING + INVOCATION PLANNING  (fast — runs on every job)
# =========================================================================== #
class FieldValue(BaseModel):
    """One planned form/body field. ``value`` is always a string; the declared type is applied
    afterwards from the discovery."""

    name: str = Field(description="A param name the analyzer DECLARES. Never invent one.")
    value: str = Field(description=(
        "The value to send. Use the literal placeholder {{question}} for the user's question, "
        "{{filename}} for the file name, or {{file_text}} for the uploaded file's text content. "
        "For a boolean param write 'true' or 'false'. Never paste the question or file text."
    ))


class PlannedInvocation(BaseModel):
    """How ONE analyzer should be called on this job, copied from its advertised contract."""

    id: str = Field(description="The analyzer id this invocation is for.")
    method: str = Field(default="POST", description="Copy EXACTLY from the analyzer's call line.")
    url: str = Field(default="", description="Copy EXACTLY from the analyzer's call line.")
    content_type: str = Field(default="", description="Copy EXACTLY from the analyzer's call line.")
    file_param: str = Field(default="", description=(
        "The declared param whose location is 'file'. Empty string if it declares none — an "
        "analyzer with a JSON contract usually takes the file's TEXT in a body param instead, "
        "via {{file_text}}."
    ))
    fields: list[FieldValue] = Field(default_factory=list, description=(
        "Every REQUIRED declared param, plus any optional one you actually need. Only declared "
        "params."
    ))
    note: str = Field(default="", description="Optional one-clause note about a chosen value.")


class RoutingDecision(BaseModel):
    """The AI Controller's answer for one job."""

    call: list[str] = Field(default_factory=list, description=(
        "ONLY the OPTIONAL analyzer ids you are adding. Mandatory analyzers always run — never "
        "list them here. Empty list if no optional analyzer is relevant."
    ))
    reason: str = Field(default="", description="One short sentence explaining the selection.")
    invocations: list[PlannedInvocation] = Field(default_factory=list, description=(
        "One entry for EVERY analyzer that will run: every mandatory analyzer, plus every id in "
        "'call'."
    ))


_ROUTING_CONTRACT = """\
You are the AI Controller for a Fortinet VISTA MCP tool. You are given the user's question, the \
input file (if any), and the analyzers available RIGHT NOW — each with what it is for and the \
exact request contract it just advertised.

Two jobs:
1. SELECT which OPTIONAL analyzers are directly relevant to THIS question. Do not add one just \
because it exists. Mandatory analyzers always run and must never appear in "call".
2. PLAN one invocation for EVERY analyzer that will run (mandatory + selected), built strictly \
from that analyzer's advertised contract.

Planning rules — follow exactly:
- Copy "method", "url" and "content_type" verbatim from the analyzer's call line. Never invent \
or modify an endpoint.
- "fields" may only contain params the analyzer DECLARES. Include every REQUIRED param.
- Use placeholders, never the content itself: {{question}} for the user's question, {{filename}} \
for the file name, {{file_text}} for the uploaded file's text.
- A required param whose value is not stated anywhere: read its description, use its default if \
it advertises one, otherwise infer the most reasonable value from the question. This is the case \
you exist for — an empty value makes the call fail.
- If an analyzer declares a file param, put its name in "file_param". If it declares none and \
takes text in the body instead, use "" and pass {{file_text}} in the relevant body field."""


async def route_and_plan(prompt: str) -> tuple[RoutingDecision | None, int, str]:
    """Select analyzers + plan every call. Returns ``(decision | None, elapsed_ms, note)``."""
    return await ai_model.run(
        RoutingDecision, _ROUTING_CONTRACT, prompt, tier="fast", label="ai_controller",
    )


# =========================================================================== #
# 2. REPORT RENDERING  (long — fallback only, for analyzers with no markdown)
# =========================================================================== #
_RENDER_CONTRACT = """\
You turn one Fortinet analyzer's raw JSON result into the markdown section a support engineer \
reads. Output MARKDOWN ONLY — no preamble, no code fence around the whole thing, no commentary \
about the JSON itself.

Rules:
- Start with a level-2 heading naming the analyzer.
- Report only what the JSON actually contains. Never invent findings, counts, versions or \
recommendations that are not in it.
- Put FortiOS CLI config in ```text fenced blocks, verbatim.
- Prefer a short prose summary plus a tight list. Keep it under 400 words unless the result \
genuinely carries more."""


class _MarkdownOut(BaseModel):
    markdown: str = Field(description="The rendered markdown section.")


async def render_report(prompt: str) -> tuple[str | None, int, str]:
    """Render a non-markdown analyzer result into a report section (fallback path)."""
    out, ms, note = await ai_model.run(
        _MarkdownOut, _RENDER_CONTRACT, prompt, tier="long", label="ai:render",
    )
    return (out.markdown if out else None), ms, note


# =========================================================================== #
# 3. TOOL CONFIG DRAFTING  (pro — the console's "build this tool with AI")
# =========================================================================== #
class DraftAnalyzer(BaseModel):
    """One analyzer entry in a drafted TOOL_ENABLEMENT tool."""

    id: str = Field(description="Short stable id. For a catalog route, its own advertised id.")
    title: str = Field(default="", description="Human title from the discovery.")
    api_url: str = Field(description=(
        "The analyzer's base URL. For an analyzer inside a CATALOG, use the catalog's base URL "
        "(the one whose /discover was fetched), not the route's own base."
    ))
    discover_url: str = Field(default="", description=(
        "Leave empty unless discovery lives somewhere other than <api_url>/discover. For a "
        "catalog route, set it to the catalog's /discover URL."
    ))
    catalog_select: str = Field(default="", description=(
        "For an analyzer that lives inside a multi-analyzer catalog, its id within that catalog. "
        "Empty for a normal single-analyzer discovery."
    ))
    mandatory: bool = Field(description=(
        "true if this analyzer must run on every call of the tool; false if the AI Controller "
        "should select it per question."
    ))
    enabled: bool = True
    timeout: float = 120


class ToolDraft(BaseModel):
    """A complete TOOL_ENABLEMENT entry drafted from a discovery document."""

    tool_name: str = Field(description=(
        "The MCP tool name clients see in list_tools. Title_Case_With_Underscores, no spaces, "
        "specific enough that a model can tell it apart from other VISTA tools."
    ))
    description: str = Field(description=(
        "The client-facing tool description — the ENTIRE basis a calling model has for choosing "
        "this tool. State what it does, what input it needs, and what it returns. It must be "
        "clearly distinguishable from a generic 'analyze a FortiGate log' tool. If the tool takes "
        "a platform-injected file, say the platform supplies it via `source_url` and the model "
        "must not fill it."
    ))
    routing_system_prompt: str = Field(description=(
        "This tool's AI Controller guidance: what the tool is for and how to decide which of its "
        "OPTIONAL analyzers to add. Write about routing only — the JSON output format is appended "
        "automatically. Two to four sentences."
    ))
    orb_enabled: bool = Field(default=False, description=(
        "Whether to append ORB troubleshooting suggestions to this tool's report. Appropriate for "
        "log/diagnostic tools; usually false for a config authoring or validation tool."
    ))
    require_source_url: bool = Field(default=True, description=(
        "false if this tool's analyzers can work from the question text alone (no uploaded file "
        "needed) — e.g. validating a pasted config snippet."
    ))
    analyzers: list[DraftAnalyzer] = Field(description="One entry per analyzer this tool uses.")
    reason: str = Field(default="", description="One sentence on how you shaped this tool.")


_DRAFT_CONTRACT = """\
You author configuration for VISTA-MCP, a Fortinet MCP server. Given an analyzer discovery \
document, produce ONE tool entry that exposes it well to a calling AI agent.

What matters most:
- The DESCRIPTION is the whole routing signal. A model picks this tool over others from that text \
alone, so it must be specific about the input it needs and the output it returns. Never reuse \
generic FortiGate-log wording for a tool that does something else.
- Set mandatory=true only for an analyzer that should run on every single call of the tool.
- If the document is a CATALOG (several analyzers under one /discover), set each entry's \
catalog_select to that analyzer's own id, and point api_url/discover_url at the CATALOG.
- Set require_source_url=false when the analyzers take text parameters rather than an uploaded \
file."""


async def draft_tool(prompt: str) -> tuple[ToolDraft | None, int, str]:
    """Draft a TOOL_ENABLEMENT entry from a discovery document (operator reviews before save)."""
    return await ai_model.run(ToolDraft, _DRAFT_CONTRACT, prompt, tier="pro", label="ai:draft")
