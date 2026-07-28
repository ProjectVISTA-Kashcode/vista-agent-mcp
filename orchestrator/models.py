"""Pydantic models for the VISTA-MCP orchestrator.

Two families of models:

1. **The standard analyzer contract** (mirrors what every VISTA analyzer emits; see
   ``docs/analyzer_api.md``): :class:`AnalyzerDiscovery` (from ``GET <base>/discover``) and
   :class:`AnalyzerResult` (from ``POST <query path>``). Because every analyzer returns these
   exact shapes, the orchestrator has ONE generic way to discover, call, and read any analyzer.

2. **Orchestrator-internal models**: :class:`ToolConfig` / :class:`AnalyzerRef` (the static
   TOOL_ENABLEMENT config), :class:`Decision` (what DeepSeek chose), and :class:`JobEvent` /
   :class:`Job` (the live flow the CLI logs and the GUI render).
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
    when_to_use: str = ""                        # what DeepSeek reads to decide
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
    ``list_tools`` shows) and its ``routing_system_prompt`` (the DeepSeek system prompt used to
    pick which optional analyzers to run for THIS tool). Both are per-tool and editable in the
    config / GUI, so a new tool tailors its own routing without touching orchestrator code.
    """

    tool_name: str
    description: str = ""                          # client-facing MCP tool description (list_tools)
    routing_system_prompt: str = ""               # per-tool DeepSeek routing system prompt section
    orb_enabled: bool = True
    analyzers: list[AnalyzerRef] = Field(default_factory=list)

    def mandatory(self) -> list[AnalyzerRef]:
        return [a for a in self.analyzers if a.enabled and a.mandatory]

    def optional(self) -> list[AnalyzerRef]:
        return [a for a in self.analyzers if a.enabled and not a.mandatory]


class Decision(BaseModel):
    """What was decided about which analyzers to call."""

    selected_ids: list[str] = Field(default_factory=list)
    used_deepseek: bool = False
    reason: str = ""
    system_prompt: str = ""                       # the per-tool routing system prompt used (GUI)
    deepseek_raw: str = ""


# --- live job / flow model (CLI logs + GUI render off these) -----------------
StepStatus = Literal["running", "ok", "skipped", "error"]


class JobEvent(BaseModel):
    """One node/edge event in the flow, timestamped."""

    step: str                                   # "pfr" | "tool_enablement" | "discover" | "decide" | "analyze:logv" | "orb" | "concat" | "done"
    status: StepStatus = "running"
    detail: str = ""
    at: float = Field(default_factory=time.time)
    elapsed_ms: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    """One tool invocation, start to finish — the unit the GUI renders."""

    job_id: str
    tool_name: str
    question: str = ""
    filename: str = ""
    status: Literal["running", "done", "error"] = "running"
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    events: list[JobEvent] = Field(default_factory=list)
    selected_analyzers: list[str] = Field(default_factory=list)
    orb_enabled: bool = False
    report_chars: int = 0
    error: str | None = None
