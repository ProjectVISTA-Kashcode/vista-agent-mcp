# VISTA Analyzer API — the standard contract

> Every VISTA analyzer (Log Visualizer today; Performance, PCAP, EoS, … tomorrow) exposes the
> **same two endpoints in the same shapes**. That is what makes VISTA-MCP modular: the
> orchestrator has **one** way to discover an analyzer, **one** way to call it, and **one** way
> to read its result — so adding an analyzer is a config edit, never new orchestrator code.
>
> This document is the source of truth for that contract. If your analyzer follows it, VISTA-MCP
> can use it with zero code changes (see `how_to_add_analyzer.md`).
>
> **Your discovery document is read on every call.** The orchestrator does not remember what your
> API looked like yesterday: it fetches `/discover`, and the **AI Controller** builds that call's
> request from what it finds there (then validates the result against the same document). So you
> can rename a param, add a required one, or move your endpoint — callers follow. See §7.

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

Tells the orchestrator **what** the analyzer is (so the AI Controller can decide whether to use it) and
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
    "when_to_use": "The text the AI Controller reads to decide. Be specific: say exactly which questions this analyzer answers and which it does NOT.",
    "input_types": ["FortiOS/FortiGate log or config file"],
    "supported_log_types": []                      // optional; [] = not log-type specific
  },
  "query": {
    "method": "POST",
    "path": "http://host/perf/run",                // fully-qualified, ready to call
    "content_type": "multipart/form-data",
    "params": [
      { "name": "file",     "required": true,  "type": "file",   "location": "file" },
      { "name": "question", "required": false, "type": "string", "location": "form" },
      { "name": "mode",     "required": true,  "type": "string", "location": "form",
        "default": "quick",
        "description": "Analysis depth: 'quick' or 'deep'. Choose 'deep' when the question names a specific device or metric." }
    ],
    "response": "AnalyzerResult"
  }
}
```

**Field notes**
- `analyzer.when_to_use` is the single most important field — it is *the* input to the AI
  Controller's selection decision. Write it like a routing rule: what to call it for, and what
  not to.
- `query.path` must be a fully-qualified URL. `query.params` declares the request body; the
  orchestrator builds every request from it, so declare **every** param you accept — a param you
  don't declare will never be sent.
- `param.description` is read by the AI Controller when it has to choose a value. Describe the
  allowed values and when each applies (see `mode` above) and it will pick sensibly.
- `param.default` (optional, additive) is the value to send when nobody has a better one. **If you
  add a REQUIRED param, give it a `default`** — that keeps the no-AI fallback path working;
  without one, a caller that can't reach the AI gateway has nothing correct to send.
- Extra fields are allowed (forward-compatible) — the orchestrator ignores what it doesn't know.
- An `ApiResponse` envelope (`{ok, kind, data:{…}}`) is tolerated: if the top level has no
  `analyzer` key but has a dict `data`, the orchestrator reads `data`.

---

### 2b. Advertising SEVERAL analyzers from one URL (a **catalog**)

A service that hosts more than one analyzer may publish them all from a single `/discover`, with
no per-route discovery endpoint. Return a **catalog** instead of a single document — the same
`AnalyzerDiscovery` objects, in an `analyzers` array:

```jsonc
{
  "schema_version": "1.0",
  "analyzers": [
    { "schema_version": "1.0", "surface": "analyzer", "base_url": "…/config-extract",
      "analyzer": { "id": "config-extract", "…": "…" }, "query": { "…": "…" } },
    { "schema_version": "1.0", "surface": "analyzer", "base_url": "…/config-validate",
      "analyzer": { "id": "config-validate", "…": "…" }, "query": { "…": "…" } }
  ]
}
```

This is what ORB does at `https://vista.fortinet.com/orb/discover`. The orchestrator detects the
shape automatically (an `analyzers` array and no top-level `analyzer` key). A config entry then
either **selects** one route or **expands** into all of them:

| `catalog_select` | result |
|---|---|
| `"config-validate"` | that one analyzer, keeping the entry's own `id` |
| `""` and the entry's `id` matches a route id | that route |
| `""` and no id match | **every** route, as `<entry id>:<route id>`, inheriting mandatory/enabled/timeout |

Nothing else changes: a selected route is discovered, planned, called, and reported exactly like
a standalone analyzer.

---

## 3. `POST <query.path>` → `AnalyzerResult`

Multipart **or JSON** request — whatever your `query.content_type` and `query.params` declare. The
response is the standard result the orchestrator concatenates:

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
- **`report_markdown` is the contract, and it is what you should send.** It is the analyzer's
  finished, human-readable contribution. The orchestrator concatenates each analyzer's
  `report_markdown` (separated by a rule) and never re-formats it — so format it fully here,
  including your own section heading.
- **If you cannot send markdown**, send `reasoning` (a prose summary) and `result` (your
  structured output) and the orchestrator will render a section for you — deterministically where
  it recognises the shape, otherwise with the `long` model. This is a *fallback*, not a target:
  you know your own output best, and a rendered section is never as good as one you wrote.
  Recognised keys inside `result`: `snippets[]` (rendered as fenced config blocks, with
  `config_path` as the label), `counts`/`structural` (flat maps → bullet lists), any other list
  (→ a bullet list), `note` (→ prose). See `orchestrator/report.py`.
- **Always return 2xx with `ok`** where you can. On a handled error, return `ok: false` with a
  short `error` and a one-line `report_markdown` explaining it — don't 500. (The orchestrator is
  fail-soft either way, but a clean result is friendlier.)
- **Declare your param types honestly.** `type: "boolean"` / `"integer"` / `"number"` are
  respected: the orchestrator casts the planned value to your declared type, so a JSON contract
  receives a real `true`, not the string `"true"`. Advertising a `default` in its real JSON type
  is fine and encouraged — it is what keeps the no-AI fallback path correct.
- **A JSON contract can still receive the uploaded file.** Declare a body param for the content
  (`config_snippet`, `config_text`, `content`, …) and the orchestrator supplies the file's *text*
  there via the `{{file_text}}` placeholder — no multipart part is involved.
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
    description: str = ""       # the AI Controller reads this when choosing a value
    default: str = ""           # value to send when nobody has a better one

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
DISCOVER   GET <base>/discover                     → AnalyzerDiscovery   (every run, concurrent)
DECIDE     AI Controller reads analyzer.when_to_use → include this analyzer?
PLAN       AI Controller reads query.{method,path,content_type,params}
                                                   → a CallPlan for each analyzer that will run
VALIDATE   the plan is checked field-by-field against that same discovery document
CALL       execute the plan                        → AnalyzerResult      (concurrent with others)
CONCAT     join every report_markdown with a horizontal rule
ORB        (once, if the tool has orb_enabled) appended as "## ORB Suggestions"
```

- **Mandatory** analyzers are always called; **optional** ones are chosen by the AI Controller
  from `when_to_use`. Both are set per tool in `config/tool_enablement.json`.
- The AI Controller runs on **every** call — including when a tool has no optional analyzers —
  precisely so a contract change is noticed the moment it happens.
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

---

## 7. Changing your API without breaking callers

This is the point of reading discovery on every call. You may:

| Change | What the caller does | What you should do |
|---|---|---|
| Rename the file param (`file` → `payload`) | attaches the file to the newly declared file param | just declare it |
| Rename the question param (`question` → `query`) | sends the question to the new name | keep `description` clear |
| Move the endpoint (`/run` → `/v2/analyze`) | calls the newly advertised `query.path` | keep the old path alive briefly if other clients exist |
| Add an **optional** param | omitted unless the AI Controller sees a reason to set it | describe when it matters |
| Add a **required** param | the AI Controller chooses a value from your `description`; the no-AI fallback sends your `default` | **always give it a `default`** |
| Change `content_type` to `application/json` | sends a JSON body built from the declared params | note that a JSON body carries no file |

What a caller will **not** do, by design: send a param you didn't declare, call a path you didn't
advertise, or use a verb/host you didn't advertise. If the AI Controller proposes any of those,
validation replaces it with the discovered value and records the correction.

`fakes/fake_analyzer_v2.py` is a working example of *all* of the above at once — a renamed file
param, a renamed + required question param, a new required `mode` param (with a `default`), and a
different path — with no VISTA-MCP change. `docs/testing.md` §4 drives it end to end.
