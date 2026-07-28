# VISTA Analyzer API — the standard contract

> Every VISTA analyzer (Log Visualizer today; Performance, PCAP, EoS, … tomorrow) exposes the
> **same two endpoints in the same shapes**. That is what makes VISTA-MCP modular: the
> orchestrator has **one** way to discover an analyzer, **one** way to call it, and **one** way
> to read its result — so adding an analyzer is a config edit, never new orchestrator code.
>
> This document is the source of truth for that contract. If your analyzer follows it, VISTA-MCP
> can use it with zero code changes (see `how_to_add_analyzer.md`).

---

## 1. The two endpoints

| Purpose | Method + path | Returns |
|---|---|---|
| **Discovery** — what it does + how to call it | `GET  <base>/discover` | `AnalyzerDiscovery` |
| **Query** — do the analysis | `POST <query.path>` (multipart) | `AnalyzerResult` |

`<base>` is the analyzer's `api_url` from the tool config; `<query.path>` is whatever the
discovery advertises (it need not be under `<base>`). The orchestrator only hardcodes the path
to **discover** — everything else it learns from the discovery document.

Both are plain HTTP + JSON. No auth is assumed between the orchestrator and analyzers on the
internal network (add mTLS/token later if needed — it does not change these shapes).

---

## 2. `GET <base>/discover` → `AnalyzerDiscovery`

Tells the orchestrator **what** the analyzer is (so DeepSeek can decide whether to use it) and
**how** to call it.

```jsonc
{
  "schema_version": "1.0",
  "surface": "analyzer",
  "base_url": "http://host/perf",
  "analyzer": {
    "id": "perf",                                  // stable short id (also the config id)
    "title": "Performance Analyzer",
    "summary": "One-line description.",
    "when_to_use": "The text DeepSeek reads to decide. Be specific: say exactly which questions this analyzer answers and which it does NOT.",
    "input_types": ["FortiOS/FortiGate log or config file"],
    "supported_log_types": []                      // optional; [] = not log-type specific
  },
  "query": {
    "method": "POST",
    "path": "http://host/perf/run",                // fully-qualified, ready to call
    "content_type": "multipart/form-data",
    "params": [
      { "name": "file",     "required": true,  "type": "file",   "location": "file" },
      { "name": "question", "required": false, "type": "string", "location": "form" }
    ],
    "response": "AnalyzerResult"
  }
}
```

**Field notes**
- `analyzer.when_to_use` is the single most important field — it is *the* input to the DeepSeek
  tool-selection decision. Write it like a routing rule: what to call it for, and what not to.
- `query.path` must be a fully-qualified URL. `query.params` declares the multipart body; the
  orchestrator builds the request from it, so name your file field and question field here.
- Extra fields are allowed (forward-compatible) — the orchestrator ignores what it doesn't know.
- An `ApiResponse` envelope (`{ok, kind, data:{…}}`) is tolerated: if the top level has no
  `analyzer` key but has a dict `data`, the orchestrator reads `data`.

---

## 3. `POST <query.path>` → `AnalyzerResult`

Multipart request: the `file` (required) and `question` (optional), exactly as declared in
`query.params`. The response is the standard result the orchestrator concatenates:

```jsonc
{
  "schema_version": "1.0",
  "ok": true,
  "analyzer_id": "perf",
  "title": "Performance Analyzer",                 // section heading in the combined report
  "report_markdown": "# Performance Analyzer\n\n…the analyzer's contribution…",
  "artifacts": { "session_id": null, "view_url": null, "iframe_url": null },  // optional extras
  "meta": { "…": "anything useful, e.g. log_type, counts, elapsed_ms" },
  "error": null
}
```

**Rules**
- **`report_markdown` is the contract.** It is the analyzer's finished, human-readable
  contribution. The orchestrator concatenates each analyzer's `report_markdown` (separated by a
  rule) and never re-formats it — so format it fully here, including your own section heading.
- **Always return 2xx with `ok`** where you can. On a handled error, return `ok: false` with a
  short `error` and a one-line `report_markdown` explaining it — don't 500. (The orchestrator is
  fail-soft either way, but a clean result is friendlier.)
- `artifacts` is free-form structured extras (links, session ids). Optional.
- `meta` is free-form. `elapsed_ms`, `log_type`, counts are nice to include.
- Do your **own** AI/analysis internally. This result is your final say. (ORB is added once, by
  the orchestrator, at the very end — analyzers do not call ORB.)

---

## 4. Pydantic models (copy these)

The orchestrator's models live in `orchestrator/models.py`. An analyzer only needs to *emit*
these shapes — copy the two response models into your service (or generate from the JSON above):

```python
from pydantic import BaseModel, Field
from typing import Any

class AnalyzerParam(BaseModel):
    name: str; required: bool = False
    type: str = "string"        # "string" | "file"
    location: str = "form"      # "form" | "file"
    description: str = ""

class AnalyzerQuery(BaseModel):
    method: str = "POST"
    path: str
    content_type: str = "multipart/form-data"
    params: list[AnalyzerParam] = Field(default_factory=list)
    response: str = "AnalyzerResult"

class AnalyzerInfo(BaseModel):
    id: str; title: str; summary: str = ""; when_to_use: str = ""
    input_types: list[str] = Field(default_factory=list)
    supported_log_types: list[str] = Field(default_factory=list)

class AnalyzerDiscovery(BaseModel):
    schema_version: str = "1.0"; surface: str = "analyzer"; base_url: str = ""
    analyzer: AnalyzerInfo; query: AnalyzerQuery

class AnalyzerResult(BaseModel):
    schema_version: str = "1.0"; ok: bool = True
    analyzer_id: str = ""; title: str = ""; report_markdown: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
```

A minimal, complete reference implementation is `fakes/fake_analyzer.py` (a real analyzer that
follows this contract in ~60 lines).

---

## 5. How the orchestrator uses it

```
DISCOVER   GET <base>/discover                → AnalyzerDiscovery         (once per run, concurrent)
DECIDE     DeepSeek reads analyzer.when_to_use → include this analyzer?    (skipped if not optional)
CALL       POST <query.path>  (file+question)  → AnalyzerResult            (concurrent with others)
CONCAT     join every report_markdown with a horizontal rule
ORB        (once, if the tool has orb_enabled) appended as "## ORB Suggestions"
```

- **Mandatory** analyzers are always called; **optional** ones are chosen by DeepSeek from
  `when_to_use`. Both are set per tool in `config/tool_enablement.json`.
- Because the shapes are fixed, one generic function
  (`orchestrator/analyzer_client.py::call`) calls **any** analyzer, and one function
  (`orchestrator/discovery.py`) discovers **any** analyzer.

---

## 6. Versioning

- `schema_version` is on every payload. This is `1.0`. Additive fields keep the same major;
  breaking changes bump it. The orchestrator tolerates unknown fields, so additive evolution
  needs no orchestrator change.
- LogV's implementation of this contract:
  - discovery: `GET /logVisualizer/api/agent_assist/discover`
  - query:     `POST /logVisualizer/api/agent_assist/run`
  - source:    `backend/src/logviz/api/discover.py` and `…/v1/mcp_run.py`
