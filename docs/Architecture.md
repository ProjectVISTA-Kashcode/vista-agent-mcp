# VISTA-MCP — Architecture

> **One sentence:** VISTA-MCP is an MCP server whose tools are thin entry points into a
> **config-driven orchestrator** that discovers a set of standard "analyzers" on every call, lets
> an **AI Controller** decide *which* optional ones to run **and how to call each one from the
> contract it just discovered*, runs them **in parallel**, concatenates their reports, appends
> **ORB** troubleshooting, returns one answer — and records the whole thing in Postgres. There is
> **no per-analyzer code**: adding capability is a config edit, not a deploy.

- Standard analyzer contract → [`analyzer_api.md`](analyzer_api.md)
- Add an analyzer / a whole tool → [`how_to_add_analyzer.md`](how_to_add_analyzer.md)
- The operator console → [`gui_guide.md`](gui_guide.md)
- Durable job history → [`db_setup.md`](db_setup.md)
- Run & test locally → [`testing.md`](testing.md)

---

## 1. The big picture

A partner agent (the MCP **client**) connects over MCP (HTTP transport, `/mcp/`, bearer token),
calls `list_tools`, and invokes a tool such as `Log_Analyzer_Visualizer`. From the client's point
of view **nothing about the behavior changed** — it still passes a `source_url` (+ optional
`question`) and gets back one text report. Everything below is internal.

```
                                   ┌──────────────────────── VISTA-MCP server (one process) ─────────────────────────┐
   MCP client                      │                                                                                  │
  (partner agent)                  │   server.py                         orchestrator/pipeline.py                     │
      │  list_tools                │  ┌───────────┐  fetch log   ┌───────────────────────────────────────────────┐   │
      │  call Tool(source_url,q)   │  │  @mcp.tool │────────────► │ VFR ─► TOOL_ENABLEMENT ─► DISCOVER ─► AI CTRL │   │
      ├───────────────────────────┼─►│  function  │              │       (config)          (/discover)  (always) │   │
      │                            │  └───────────┘              │                                   │           │   │
      │                            │       ▲                     │       ┌──── selected, in parallel ─┴────────┐ │   │
      │      one text report       │       │  report string      │       ▼            ▼             ▼          │ │   │
      │◄──────────────────────────┼───────┴─────────────────────┤ analyzer A   analyzer B   … (async gather)  │ │   │
      │                            │                             │       └──────────────┬─────────────────────┘ │   │
      │                            │                             │           CONCATENATE │  then  ORB (if on)    │   │
      │                            │                             └───────────────────────┴───────────────────────┘   │
      │                            │   every step → jobs registry → CLI logs + /gui (live flow) + Postgres (forever)  │
      │                            └──────────────────────────────────────────────────────────────────────────────────┘
                                                    │  discover + call (standard HTTP)      │ ask
                                     ┌──────────────┼───────────────┐              ┌────────▼─────────┐
                                     ▼              ▼               ▼              │  ORB ask API      │
                              LogV AgentAssist  Perf analyzer   EoS analyzer …     │  (troubleshooting)│
                              (mandatory)       (optional)      (optional)         └───────────────────┘
```

The flow is:
**`tool → VFR → TOOL_ENABLEMENT → DISCOVER TOOLS → AI CONTROLLER → call analyzers (async) →
concatenate → ORB (if enabled) → answer back`.**

---

## 2. Three ideas make it modular

### 2a. Every analyzer speaks ONE standard contract
See [`analyzer_api.md`](analyzer_api.md). Every analyzer exposes:

- `GET  <base>/discover` → **AnalyzerDiscovery** (what it is + **how to call it**, incl. `when_to_use`)
- `POST <query.path>` (multipart **or JSON**: the declared params) → **AnalyzerResult**

Because the *shapes* are fixed, the orchestrator has exactly **one** discover function
(`discovery.py`), **one** call function (`analyzer_client.py`), and **one** read path
(`report.py`). LogV implements this contract in its own repo
(`backend/src/logviz/api/discover.py` + `…/v1/mcp_run.py`); the fakes implement it in
`fakes/fake_analyzer.py` and `fakes/fake_analyzer_v2.py`. **The orchestrator shares zero code with
any analyzer.**

Two things the contract deliberately does *not* pin down:

- **How many analyzers one URL advertises.** A `/discover` may return a single analyzer, or a
  **catalog** — `{"schema_version": "1.0", "analyzers": [ … ]}`. ORB does the latter:
  `https://vista.fortinet.com/orb/discover` advertises `config-extract` and `config-validate`
  together, and the individual routes have no `/discover` of their own. A config entry either
  picks one out of a catalog (`catalog_select`) or expands into all of them (§2c).
- **Whether a result is markdown.** An analyzer that returns `{ok, reasoning, result, meta}`
  instead of `report_markdown` is normalised into a section by `report.py` (§4f).

### 2b. The *contents* of that contract are read live, every call
The shapes are fixed; the **details are not**. An analyzer may rename a param, add a required one,
or move its endpoint — and a brand-new analyzer may be added to a tool minutes earlier. So the
orchestrator hardcodes nothing about a request: on every job it fetches each analyzer's
`/discover`, and the **AI Controller** builds a per-analyzer **call plan** from that document
(§4). The plan is then validated against the same document before it is sent.

### 2c. Everything else is in ONE config per tool
`config/tool_enablement.json` — the **TOOL_ENABLEMENT** config. One entry per MCP tool:

```jsonc
"Log_Analyzer_Visualizer": {
  "description": "…client-facing text shown in list_tools…",
  "routing_system_prompt": "…this tool's AI Controller analyzer-selection prompt…",
  "orb_enabled": true,
  "analyzers": [
    { "id": "logv", "api_url": "…/agent_assist", "mandatory": true,  "enabled": true, "timeout": 300 },
    { "id": "perf", "api_url": "…/perf",         "mandatory": false, "enabled": true, "timeout": 120 },
    { "id": "eos",  "api_url": "…/eos",          "mandatory": false, "enabled": true, "timeout": 120 }
  ]
}
```

- **`description`** — the MCP tool description clients read (config-driven so it's editable per
  tool; `server.py` falls back to a built-in default if empty).
- **`routing_system_prompt`** — **per-tool** AI Controller system prompt for the routing decision.
  Each MCP tool routes with its own tailored guidance (a config-editable "section"); a fixed
  output contract is always appended so parsing never breaks. See §5.
- **`orb_enabled`** — static decision (not the AI Controller): run ORB once at the end if true.
- **`analyzers[]`** — `mandatory` ones are always called; the rest are AI-Controller-selected.
  `discover_url` empty ⇒ derived as `api_url + "/discover"`.

**To add an analyzer to a tool, or add a whole new tool, you edit this file (or the GUI) — no
orchestrator code changes.**

---

## 3. The pipeline, step by step

`orchestrator/pipeline.py::run(tool_name, file_bytes, filename, question, job_id)` → `str`.
Every step emits a job event (CLI log + GUI + database). The whole function is wrapped so a bug
never escapes as a raw MCP error; it always returns a string.

1. **INTAKE** — the tool call as received plus the fetched file, so every execution shows what was
   called and which file came in.

2. **VFR** (`vfr.py`) — *vetting & flow routing*. **Passthrough today**: takes the file, returns
   the same file, reports `"Correct analyzer match"`. It is the seam where real routing logic goes
   later (see §10). Kept as its own step so the flow/telemetry already has the node.

3. **TOOL_ENABLEMENT** (`tool_enablement.py`) — load this tool's `ToolConfig` (cached; the GUI can
   hot-reload it). Split analyzers into `mandatory()` and `optional()`. If the tool has no
   config/analyzers, return a friendly message.

4. **DISCOVER TOOLS** (`discovery.py`) — `GET <base>/discover` for **every** configured analyzer,
   **concurrently**, fail-soft. An analyzer that doesn't answer is dropped (logged) and simply not
   used this run. Tolerates an `ApiResponse` envelope (`data:{…}`), and understands **both**
   discovery shapes: a single analyzer, or a **catalog** of several. A catalog entry resolves to
   one analyzer (`catalog_select`) or **expands** into one runtime analyzer per route, named
   `<entry id>:<route id>`, each inheriting the entry's mandatory/enabled/timeout. So the set that
   runs is not necessarily the set in the config, and the step returns the expanded **runtime**
   refs the rest of the pipeline works on. This is the only source of truth for what each
   analyzer currently is and how it is called.

5. **AI CONTROLLER** (`decide.py` + `ai_controller.py` + `plan.py`) — **always runs** (§4). It
   returns a `Decision`: the selected analyzer ids, the reason, and a validated **`CallPlan` per
   analyzer that will run**.

6. **CALL analyzers** — the selected analyzers run **concurrently** via `asyncio.gather`, each
   through the single generic `analyzer_client.call(ref, discovery, file, filename, question,
   plan)` which **executes the validated plan** and parses the response into `AnalyzerResult`. It
   **never raises** — on failure it returns `ok=False` with an error result, so one slow/broken
   analyzer can't sink the others.

7. **CONCATENATE** — join every non-empty `report_markdown` with a horizontal rule
   (`\n\n---\n\n`). The orchestrator never re-formats an analyzer's text.

8. **ORB** (`orb.py`) — if `orb_enabled`, ask the ORB troubleshooting API **once** with the
   combined report + the user's question, and append the answer as `## ORB Suggestions`.
   **Fail-open:** if ORB errors or is empty, the report is returned without it.

9. **DONE** — return the combined markdown. `jobs.finish()` records totals, and the whole job
   (steps, per-analyzer detail, final report) is persisted.

---

## 4. The AI Controller

Formerly "the DeepSeek decision". Renamed because it does more than pick analyzers, and because
the gateway behind it is an implementation detail. It is still **only** a control-plane call — the
analysis itself always belongs to the analyzers.

### 4·0. It runs on Pydantic AI

Every AI step is a **typed [Pydantic AI](https://ai.pydantic.dev) agent with a declared output
model**, run against the Fortinet **AgentAssist** gateway — an OpenAI-compatible endpoint
(`/v1/chat/completions`, vLLM behind it) that supports both `response_format: json_schema` and
native tool calling.

That means the orchestrator never hand-parses model text. The answer arrives already validated
into a Pydantic object, and Pydantic AI re-prompts the model itself when it doesn't fit the
schema. The old "ask for JSON, regex the first `{…}` out of the reply, hope" path is gone.

**Three model tiers**, chosen per task rather than one model for everything
(`orchestrator/ai_model.py`):

| tier | default model | used for | typical |
|---|---|---|---|
| `fast` | `model-fast` | routing + invocation planning — runs on **every** job | ~0.7 s |
| `pro`  | `model-pro`  | authoring — drafting a whole tool config from a discovery document | ~4 s |
| `long` | `model-long` | large inputs — rendering a big analyzer result into markdown | varies |

One module builds every model, so all of them share the provider, the timeout, and — via
`tlsconf` — the *same outbound TLS policy* as every other call the server makes. An internal-CA
gateway needs no special case.

**Three agents** (`orchestrator/ai_controller.py`):

| agent | tier | job |
|---|---|---|
| `route_and_plan` | fast | §4a — which analyzers, and how to call each |
| `render_report`  | long | §4f — a result that carries no `report_markdown` |
| `draft_tool`     | pro  | §4g — a whole tool entry from a discovery URL |

Env: `AGENTASSIST_BASE_URL`, `AGENTASSIST_API_KEY`, `AI_MODEL_FAST` / `_PRO` / `_LONG`,
`AI_TIMEOUT`, `AI_TEMPERATURE` (0 — these are decisions, not prose). `AI_CONTROLLER_ENABLED=0`,
**or simply an unset API key**, forces the fully deterministic path (§4e).

### 4a. It has two jobs

1. **Selection** — which *optional* analyzers add value for this question (mandatory ones are
   always in the set), judged from each analyzer's `when_to_use` in its live discovery.
2. **Invocation planning** — for **every** analyzer that will run, how to call it: method, URL,
   content type, which param carries the file, which declared params get the question, and what
   value any other required param should take. All read from the contract just discovered.

### 4b. Why it never skips

The old rule was "0 optional analyzers ⇒ the answer is determined ⇒ skip the model". That was true
only while a request shape could be assumed. It can't: discovery is live, an analyzer can change
its API between two calls, and a new analyzer can appear in the config at any moment. Skipping
would mean calling a possibly-changed API with a request shape someone assumed the last time they
read the docs.

So the controller runs on **every** job. With nothing to select it runs in **`plan-only`** mode and
still reads the fresh contracts. Discovery decides *what is possible*; the controller decides
*what to do about it*. The GUI shows the mode on the node.

### 4c. The prompt

```
   <tool's routing_system_prompt   (or the generic default if unset)>   ← per tool, editable
   +
   <fixed agent CONTRACT: the planning rules>                           ← machinery
   +
   <the input file name/size (or "none"), the user's question>
   +
   <every analyzer's LIVE contract: when_to_use, method, URL, content type,
    and each declared param: name, required, type, location, default, description>
```

The response *shape* is no longer part of the prompt at all — it is the agent's Pydantic output
model, enforced by the schema. So a tool author can rewrite the *guidance* freely, to justify and
match that tool's description, with **no way to break the decision parsing**. The exact prompt
used is stored on the `Decision`, surfaced on the AI Controller node in the GUI, and written to
the database.

Field values use **placeholders** rather than echoing content, so a long question — or a whole
config file — can never be truncated, reworded or re-quoted on its way to an analyzer:

| placeholder | resolves to |
|---|---|
| `{{question}}` | the user's question |
| `{{filename}}` | the uploaded file's name |
| `{{file_text}}` | the uploaded file's **text** — for a JSON-contract analyzer that takes content in a body param instead of as a multipart upload |

Planned field *values* are always typed `str` in the schema, even for a boolean or integer param.
Free-form JSON values are the part of a structured answer models get wrong most often, and the
real type is already in the discovery — so the cast happens deterministically in
`AnalyzerParam.coerce`, not in the model's head.

### 4d. Validation — the model never gets the last word

`plan.py::from_ai` checks every field of the proposed plan against the same discovery document:

| Field | Rule |
|---|---|
| `method` | must equal the discovered method, else corrected |
| `url` | must equal the discovered `query.path`, else corrected (a different host is rejected outright) |
| `content_type` | must equal the discovered content type, else corrected |
| `file_param` | must be a declared `location:"file"` param, else the first declared one |
| `fields` | only declared params survive; unknown keys are dropped |
| field types | each surviving value is cast to the param's **declared** type (`boolean` → real `true`, not `"true"`) |
| required params | anything missing is filled from the discovery (its `default`, else the question/filename/file-text heuristics) |

Every correction is recorded in `CallPlan.notes`, shown on the node in the GUI, and stored per
analyzer per job — so "what did the model propose vs. what did we send" is always answerable.
The plan's `source` ends up `ai`, `ai-corrected` or `deterministic`.

### 4e. Fail-safe

If the AI gateway is disabled (`AI_CONTROLLER_ENABLED=0` or no API key), unreachable, or answers
unusably, the decision falls back to the **deterministic policy**: include every optional analyzer
(better to over-analyze than to silently drop a relevant one) and build each request straight from
its discovery document (`plan.py::deterministic`). For the standard `file` + `question` contract
that produces byte-for-byte the request VISTA-MCP has always sent — asserted by
`tests/test_orchestrator.py::logv_deterministic_plan_is_unchanged` — so a gateway outage is
invisible to clients. It costs breadth of judgement, never correctness.

Every AI entry point returns `None` on any failure and never raises. That guarantee is the reason
the AI can be central without being load-bearing.

### 4f. Rendering a result that has no markdown

The contract says an analyzer returns `report_markdown`. Real ones don't always — ORB's routes
return `{ok, reasoning, result, meta}` and nothing else. Before this, such a result validated
fine and contributed an **empty** section, so a job whose only analyzer was ORB reported "No
analyzer produced a result."

`orchestrator/report.py` normalises any payload into a section, in three tiers — first one that
yields text wins:

1. **`native`** — `report_markdown` is present. Returned untouched. *This is the LogV path; nothing
   below can affect it.*
2. **`rendered`** — a deterministic renderer walks the result. It knows the shapes VISTA analyzers
   emit (`reasoning`, `snippets`, `counts`, `flagged`, `structural`) and falls back to a generic
   recursive walk. **No AI**, so a new analyzer works with the controller switched off.
3. **`ai`** — the `render_report` agent on the *long* model. Used only when tier 2 comes out thin
   against a payload that clearly carries more (`AI_REPORT_RENDER=auto`, the default), always
   (`=1`), or never (`=0`).

Which tier produced a section is recorded on the analyzer's `meta.report_source`.

### 4g. Drafting a tool from a discovery URL

The console's **✨ Build with AI** button posts a discovery URL to `/gui/api/ai_build_tool`. The
server probes it — single analyzer *or catalog* — hands the real document to the `pro` model, and
gets back a complete TOOL_ENABLEMENT entry: tool name, client-facing description, routing prompt,
and one analyzer entry per route.

The draft is then **validated against the discovery it was drafted from**, exactly as a call plan
is (§4d): a `catalog_select` naming a route that doesn't exist is re-matched or dropped, a
`discover_url` that isn't a `/discover` endpoint is normalised or cleared, empty titles are filled
from the document. Corrections are returned as `fixes` and shown in the dialog.

**Nothing is saved.** The draft lands in the editor for review and only reaches `list_tools` via
the normal validated + audited `POST /gui/api/config` save.

---

## 5. Per-tool routing prompt (why it exists)

Different MCP tools route differently. A log tool weighs "does this question need the performance
or EoS companion?"; a future config-generation tool would weigh entirely different companions. So
the **routing system prompt is per tool**, stored in TOOL_ENABLEMENT next to the analyzers, and
editable in the GUI. `decide.py` composes it with the fixed output contract (§4c).

---

## 6. Concurrency model

- **Across tool calls:** every call gets its own `job_id`, its own async context, and its own
  entry in the job registry. The server (uvicorn/Starlette under FastMCP) handles many clients and
  files at once. Verified with 3 simultaneous clients routing three different ways.
- **Within a call:** discovery is concurrent; the selected analyzers are concurrent
  (`asyncio.gather`); ORB is a single call at the end. Wall-clock ≈ AI Controller + slowest
  analyzer + ORB, not the sum.
- **Database writes** never touch the request path: they are queued and drained by one background
  thread (§8).

---

## 7. Observability — CLI logs, the console, and history

- **`orchestrator/jobs.py`** is the single source of truth. `emit()` does three things at once:
  writes a structured line to the terminal (via `vlog`, with icons, timings and details), appends
  a `JobEvent` to the in-memory `Job`, and queues the event for Postgres.
- **`orchestrator/gui.py`** attaches the operator console to the same process via FastMCP custom
  routes (so it reads the **live** in-memory registry — no polling a file). Mounted under **both
  `/gui` and `/mcp/gui`** (the latter so it's reachable behind the prod proxy that forwards only
  `/mcp/*`); the page derives its API base from its own URL:
  - `GET /gui` — the single-page console (`gui/index.html`)
  - `GET …/gui/api/state` — tools (from config) + **live** job summaries + DB/server status
  - `GET …/gui/api/jobs` — **durable** history from Postgres, filtered + paginated
  - `GET …/gui/api/jobs/{id}` — one job's flow: live from memory, else rebuilt from Postgres
  - `GET …/gui/api/analytics` — dashboard aggregates
  - `GET …/gui/api/facets` — distinct tools/analyzers seen (history filters)
  - `GET|POST …/gui/api/config` — read / **save** TOOL_ENABLEMENT (validated, hot-reloaded, audited)
  - `POST …/gui/api/probe` — fetch an analyzer's `/discover` so the editor can validate + auto-fill
- The console has three views — **Flow** (live canvas with pan/zoom and a per-node inspector),
  **Dashboard** (analytics over the stored history), **History** (search the permanent record and
  replay any job). Full walkthrough: [`gui_guide.md`](gui_guide.md).
- **Dynamic tools:** `server.py` registers one generic MCP tool per config entry and reconciles
  the live tool set via `sync_tools()` at startup and on every save (through
  `tool_enablement.on_change`). So **adding a tool in the GUI exposes a real MCP tool — name +
  endpoints — with no code and no restart**; removing one unregisters it. All VISTA tools share
  the `source_url`+`question` surface, so one function serves every tool.

> **Security note:** the `/gui` routes are **not** behind the MCP bearer token — it's a local
> operator console. Bind to localhost/a trusted network, or front it with your own auth. The
> config POST validates before writing (and records an audit row); the probe POST performs a
> server-side GET on the URL you type (http/https only).

---

## 8. Durable job history

`orchestrator/db.py` writes every job to PostgreSQL — see [`db_setup.md`](db_setup.md) for the
schema, the production `psql` commands and the environment variables.

| Table | One row per |
|---|---|
| `mcp_jobs` | tool call — status, timings, routing, AI prompt/answer/plan source, ORB, final report |
| `mcp_job_events` | pipeline step event, in order |
| `mcp_job_analyzers` | analyzer per job — its live discovery, the exact request sent, the result |
| `mcp_config_audit` | TOOL_ENABLEMENT save from the GUI |

Design rules: **never block the pipeline** (writes go on an in-process queue drained by one
background thread), **never break a request** (every failure is caught and logged; with no
database the server behaves exactly as it did before), and **one writer, in order** (a job row
always lands before its events). The in-memory registry remains as the *live* window
(`MCP_JOBS_MEMORY`, default 60); Postgres is the permanent record.

---

## 9. Security posture (unchanged by the rearchitecture)

- **Auth:** MCP protocol endpoint requires a static bearer token (`StaticTokenVerifier`). The
  server refuses to start with an empty/default token unless `MCP_ALLOW_INSECURE=1`.
- **SSRF guard:** the injected `source_url` is fetched server-side; non-public targets are
  rejected on the initial request and on every redirect hop.
- **Streaming + capped fetch:** logs are streamed and size-capped (`MAX_LOG_BYTES`); signed query
  strings are never logged.
- **TLS:** verified by default; opt-in relaxation is host-scoped (`tlsconf.py`).
- **The AI Controller cannot widen the blast radius:** it can only propose a request, and a
  proposal that isn't in the analyzer's own discovery document is corrected or dropped before
  anything is sent (§4d).

---

## 10. File map

```
server.py                     MCP server: auth, log fetch (SSRF/TLS), one thin tool fn → pipeline; mounts GUI
config/tool_enablement.json   TOOL_ENABLEMENT — per-tool: description, routing_system_prompt, orb, analyzers[]
orchestrator/
  models.py                   AnalyzerDiscovery/AnalyzerResult (contract) + ToolConfig/AnalyzerRef/
                              CallPlan/Decision/AnalyzerRun/Job
  tool_enablement.py          load/get/reload/save the config (cached, safe default)
  vfr.py                      VFR — passthrough today (real routing seam; see below)
  discovery.py                GET /discover for all analyzers, concurrent, fail-soft (+ probe());
                              understands a SINGLE analyzer or a CATALOG of several
  ai_model.py                 the Pydantic AI foundation: provider, fast/pro/long tiers, fail-safe run()
  ai_controller.py            the three typed agents: route_and_plan · render_report · draft_tool
  decide.py                   the AI Controller decision: selection + per-analyzer invocation plans
  plan.py                     build a CallPlan from discovery · validate an AI plan against it
  analyzer_client.py          the ONE generic call — executes a validated plan (never raises)
  report.py                   normalise any analyzer result into a markdown section (native/rendered/ai)
  orb.py                      ORB ask (fail-open) → "## ORB Suggestions"
  pipeline.py                 the whole flow, wired together, always returns a string
  jobs.py                     job/flow registry → CLI logs + GUI + database
  db.py                       durable history in Postgres (queued, one writer, fail-open)
  gui.py                      /gui routes (state, history, job flow, analytics, config, probe)
gui/index.html                the operator console (Flow · Dashboard · History)
fakes/fake_analyzer.py        a reference analyzer following the contract
fakes/fake_analyzer_v2.py     the same contract with a DIFFERENT request shape (dynamic-discovery test)
tests/test_orchestrator.py    runnable checks: LogV regression floor · catalogs · typing · rendering · live
docs/                         analyzer_api.md · Architecture.md · how_to_add_analyzer.md ·
                              gui_guide.md · db_setup.md (+ .sql) · testing.md
```

---

## 11. Next improvements

- **Real VFR logic (highest priority).** `vfr.py` is a passthrough today (returns the file and
  `"Correct analyzer match"`). The seam is already in the flow, the GUI, the telemetry and the
  database, so the real implementation is a drop-in. Intended responsibilities:
  - **Pre-flight file inspection / normalization** — sniff format, decompress, size/shape checks,
    redact obvious secrets before anything leaves the process.
  - **Analyzer-match gating** — confirm the file actually suits this tool's analyzers and return a
    real match verdict (today it always says "Correct analyzer match"). A negative verdict could
    short-circuit with a helpful message instead of calling analyzers.
  - **Routing hints** — emit structured hints (detected log/event type, product, version) that the
    AI Controller can use to sharpen selection.
  - Keep it fail-safe and cheap; it runs on every call before any analyzer.
- **Discovery cache** with a short TTL so `/discover` isn't hit on every call — must stay short
  enough that a contract change is picked up promptly.
- **AI Controller decision cache** keyed by (tool, *discovery fingerprint*, normalized question).
  Fingerprinting the discovery keeps it correct: any contract change busts the cache.
- **Auth on the GUI** (token or SSO) for non-local deployments.
- **Per-analyzer streaming/partial results** to the console as they complete.
- **Config schema validation** surfaced inline in the GUI editor (field-level errors).
