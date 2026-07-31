"""Pydantic models for the VISTA-MCP orchestrator.

Two families of models:

1. **The standard analyzer contract** (mirrors what every VISTA analyzer emits; see
   ``docs/analyzer_api.md``): :class:`AnalyzerDiscovery` (from ``GET <base>/discover``) and
   :class:`AnalyzerResult` (from ``POST <query path>``). Because every analyzer returns these
   exact shapes, the orchestrator has ONE generic way to discover, call, and read any analyzer.

2. **Orchestrator-internal models**: :class:`ToolConfig` / :class:`AnalyzerRef` (the static
   TOOL_ENABLEMENT config), :class:`CallPlan` + :class:`Decision` (what the **AI Controller**
   chose *and* how it decided to invoke each analyzer, straight from the live discovery), and
   :class:`JobEvent` / :class:`AnalyzerRun` / :class:`Job` (the flow the CLI logs, the GUI
   renders, and the database persists).
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


# ===========================================================================
# 1. THE STANDARD ANALYZER CONTRACT (shared by every analyzer)
# ===========================================================================
class AnalyzerParam(BaseModel):
    name: str
    required: bool = False
    type: str = "string"          # string | file
    location: str = "form"        # form | file
    description: str = ""
    # Optional (additive, v1.0-compatible). If an analyzer adds a REQUIRED param, advertising a
    # default here keeps even the no-AI fallback path correct: the deterministic builder sends
    # this value, and the AI Controller may override it when the question calls for something
    # else. Analyzers that omit it rely on the AI Controller to choose a value.
    default: str = ""


class AnalyzerQuery(BaseModel):
    """How to call an analyzer's one query endpoint (from its /discover)."""

    method: str = "POST"
    path: str                                   # fully-qualified URL, ready to call
    content_type: str = "multipart/form-data"
    params: list[AnalyzerParam] = Field(default_factory=list)
    response: str = "AnalyzerResult"


class AnalyzerInfo(BaseModel):
    id: str
    title: str
    summary: str = ""
    when_to_use: str = ""                        # what the AI Controller reads to decide
    input_types: list[str] = Field(default_factory=list)
    supported_log_types: list[str] = Field(default_factory=list)


class AnalyzerDiscovery(BaseModel):
    """The full /discover document. Extra fields are tolerated (forward-compatible)."""

    model_config = {"extra": "allow"}
    schema_version: str = SCHEMA_VERSION
    surface: str = ""
    base_url: str = ""
    analyzer: AnalyzerInfo
    query: AnalyzerQuery


class AnalyzerArtifacts(BaseModel):
    model_config = {"extra": "allow"}
    session_id: str | None = None
    view_url: str | None = None
    iframe_url: str | None = None


class AnalyzerResult(BaseModel):
    """The standard result every analyzer returns from its query endpoint."""

    model_config = {"extra": "allow"}
    schema_version: str = SCHEMA_VERSION
    ok: bool = True
    analyzer_id: str = ""
    title: str = ""
    report_markdown: str = ""
    artifacts: AnalyzerArtifacts = Field(default_factory=AnalyzerArtifacts)
    meta: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


# ===========================================================================
# 2. ORCHESTRATOR-INTERNAL MODELS
# ===========================================================================
class AnalyzerRef(BaseModel):
    """One entry in a tool's TOOL_ENABLEMENT config."""

    id: str                                     # short id, e.g. "logv", "perf"
    title: str = ""
    api_url: str                                # analyzer base, e.g. https://…/agent_assist
    discover_url: str = ""                       # empty → derived as api_url + "/discover"
    mandatory: bool = False                     # mandatory analyzers are ALWAYS called
    enabled: bool = True
    timeout: float = 300.0

    def resolved_discover_url(self) -> str:
        return self.discover_url or (self.api_url.rstrip("/") + "/discover")


class ToolConfig(BaseModel):
    """The TOOL_ENABLEMENT config for ONE MCP tool (e.g. Log_Analyzer_Visualizer).

    Everything about a tool is here — including its client-facing ``description`` (what
    ``list_tools`` shows) and its ``routing_system_prompt`` (the AI Controller system prompt used
    to pick which optional analyzers to run for THIS tool). Both are per-tool and editable in the
    config / GUI, so a new tool tailors its own routing without touching orchestrator code.
    """

    tool_name: str
    description: str = ""                          # client-facing MCP tool description (list_tools)
    routing_system_prompt: str = ""               # per-tool AI Controller routing prompt section
    orb_enabled: bool = True
    analyzers: list[AnalyzerRef] = Field(default_factory=list)

    def mandatory(self) -> list[AnalyzerRef]:
        return [a for a in self.analyzers if a.enabled and a.mandatory]

    def optional(self) -> list[AnalyzerRef]:
        return [a for a in self.analyzers if a.enabled and not a.mandatory]


class CallPlan(BaseModel):
    """**How** one analyzer is invoked on this run — derived from its LIVE discovery.

    This is the dynamic half of the AI Controller. The orchestrator hardcodes nothing about an
    analyzer's request: the AI Controller reads the analyzer's freshly-discovered ``query``
    contract (method, path, content type, declared params) and emits a plan; the plan is then
    *validated against that same discovery* before it is executed, so a hallucinated or stale
    plan can never produce a call the analyzer didn't advertise.

    ``source`` records where the executed plan came from:

    * ``ai``             — the AI Controller's plan, used verbatim
    * ``ai-corrected``   — the AI Controller's plan after validation fixed a field (see ``notes``)
    * ``deterministic``  — the built-from-discovery fallback (AI off/unavailable/unusable)
    """

    analyzer_id: str = ""
    method: str = "POST"
    url: str = ""
    content_type: str = "multipart/form-data"
    file_param: str = ""                          # param that carries the file ("" = no file)
    fields: dict[str, str] = Field(default_factory=dict)   # form/JSON fields (placeholders resolved)
    source: str = "deterministic"                 # ai | ai-corrected | deterministic
    notes: list[str] = Field(default_factory=list)         # what validation changed, and why
    ai_note: str = ""                             # the AI Controller's own note for this call


class Decision(BaseModel):
    """What the AI Controller decided: which analyzers run, and how each one is called."""

    selected_ids: list[str] = Field(default_factory=list)
    used_ai: bool = False                         # did the AI Controller actually answer?
    reason: str = ""
    system_prompt: str = ""                       # the per-tool routing system prompt used (GUI)
    ai_raw: str = ""                              # raw AI answer (audit trail)
    plans: dict[str, CallPlan] = Field(default_factory=dict)   # analyzer_id -> how to call it
    plan_source: str = "deterministic"            # ai | ai-corrected | deterministic | mixed
    elapsed_ms: int | None = None
    mode: str = "select+plan"                     # select+plan | plan-only | disabled


# --- live job / flow model (CLI logs + GUI render + DB persistence off these) -----------------
StepStatus = Literal["running", "ok", "skipped", "error"]


class JobEvent(BaseModel):
    """One node/edge event in the flow, timestamped."""

    step: str                                   # "intake" | "vfr" | "tool_enablement" | "discover" | "ai_controller" | "analyze:logv" | "concat" | "orb" | "done"
    status: StepStatus = "running"
    detail: str = ""
    at: float = Field(default_factory=time.time)
    elapsed_ms: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AnalyzerRun(BaseModel):
    """Everything about ONE analyzer on ONE job — config, discovery, request, result.

    Persisted per job so history can answer "what exactly did we send to this analyzer, what did
    its API look like at the time, and what did it return?" long after the run.
    """

    analyzer_id: str
    title: str = ""
    api_url: str = ""
    mandatory: bool = False
    discovered: bool = False
    selected: bool = False
    called: bool = False
    ok: bool | None = None
    when_to_use: str = ""
    discovery: dict[str, Any] = Field(default_factory=dict)   # the live /discover document
    plan: CallPlan | None = None                              # the executed request plan
    elapsed_ms: int | None = None
    report_chars: int = 0
    report_markdown: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class Job(BaseModel):
    """One tool invocation, start to finish — the unit the GUI renders and the DB stores."""

    job_id: str
    tool_name: str
    question: str = ""
    filename: str = ""
    file_bytes: int = 0
    status: Literal["running", "done", "error"] = "running"
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    events: list[JobEvent] = Field(default_factory=list)

    # --- routing / enablement ---
    mandatory_ids: list[str] = Field(default_factory=list)
    optional_ids: list[str] = Field(default_factory=list)
    discovered_ids: list[str] = Field(default_factory=list)
    dropped_ids: list[str] = Field(default_factory=list)
    selected_analyzers: list[str] = Field(default_factory=list)

    # --- AI Controller ---
    used_ai: bool = False
    ai_mode: str = ""                             # select+plan | plan-only | disabled
    ai_reason: str = ""
    ai_system_prompt: str = ""
    ai_raw: str = ""
    ai_plan_source: str = ""
    ai_elapsed_ms: int | None = None

    # --- ORB ---
    orb_enabled: bool = False
    orb_status: str = ""                          # ok | skipped | error | off
    orb_chars: int = 0
    orb_elapsed_ms: int | None = None

    # --- result ---
    report_chars: int = 0
    report_markdown: str = ""
    analyzers: list[AnalyzerRun] = Field(default_factory=list)
    error: str | None = None
