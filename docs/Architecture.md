# VISTA-MCP — Architecture

> **One sentence:** VISTA-MCP is an MCP server whose tools are thin entry points into a
> **config-driven orchestrator** that discovers a set of standard "analyzers," lets **DeepSeek**
> pick which optional ones to run for the user's question, calls them **in parallel**,
> concatenates their reports, appends **ORB** troubleshooting, and returns one answer — with
> **no per-analyzer code**. Adding capability is a config edit, not a deploy.

- Standard analyzer contract → [`analyzer_api.md`](analyzer_api.md)
- Add an analyzer / a whole tool → [`how_to_add_analyzer.md`](how_to_add_analyzer.md)
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
      │  list_tools                │  ┌───────────┐  fetch log   ┌──────────────────────────────────────────────┐    │
      │  call Tool(source_url,q)   │  │  @mcp.tool │────────────► │  VFR ─► TOOL_ENABLEMENT ─► DISCOVER ─► DECIDE │    │
      ├───────────────────────────┼─►│  function  │              │        (config)          (/discover) (DeepSeek)│   │
      │                            │  └───────────┘              │                                   │            │    │
      │                            │       ▲                     │        ┌──────── selected, in parallel ───────┐│    │
      │      one text report       │       │  report string      │        ▼            ▼             ▼            ││    │
      │◄──────────────────────────┼───────┴─────────────────────┤  analyzer A   analyzer B   … (async gather)   ││    │
      │                            │                             │        └──────────────┬──────────────────────┘│    │
      │                            │                             │            CONCATENATE │  then  ORB (if on)    │    │
      │                            │                             └────────────────────────┴───────────────────────┘   │
      │                            │   every step → jobs registry → CLI logs + /gui (n8n-style live flow)              │
      │                            └──────────────────────────────────────────────────────────────────────────────────┘
                                                    │  discover + call (standard HTTP)      │ ask
                                     ┌──────────────┼───────────────┐              ┌────────▼─────────┐
                                     ▼              ▼               ▼              │  ORB ask API      │
                              LogV AgentAssist  Perf analyzer   EoS analyzer …     │  (troubleshooting)│
                              (mandatory)       (optional)      (optional)         └───────────────────┘
```

The flow matches the architecture diagram exactly:
**`tool → VFR → TOOL_ENABLEMENT → DISCOVER TOOLS → DEEPSEEK QUERY → call analyzers (async) →
concatenate → ORB (if enabled) → answer back`.**

---

## 2. Two ideas make it modular

### 2a. Every analyzer speaks ONE standard contract
See [`analyzer_api.md`](analyzer_api.md). Every analyzer exposes:

- `GET  <base>/discover` → **AnalyzerDiscovery** (what it is + how to call it, incl. `when_to_use`)
- `POST <query.path>` (multipart: file + optional question) → **AnalyzerResult** (`report_markdown`)

Because the shapes are fixed, the orchestrator has exactly **one** discover function
(`discovery.py`), **one** call function (`analyzer_client.py`), and **one** read path
(`report_markdown`). LogV implements this contract in its own repo
(`backend/src/logviz/api/discover.py` + `…/v1/mcp_run.py`); the fakes implement it in
`fakes/fake_analyzer.py`. **The orchestrator shares zero code with any analyzer.**

### 2b. Everything else is in ONE config per tool
`config/tool_enablement.json` — the **TOOL_ENABLEMENT** config. One entry per MCP tool:

```jsonc
"Log_Analyzer_Visualizer": {
  "description": "…client-facing text shown in list_tools…",
  "routing_system_prompt": "…this tool's DeepSeek analyzer-selection prompt…",
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
- **`routing_system_prompt`** — **per-tool** DeepSeek system prompt for the routing decision.
  Each MCP tool routes with its own tailored guidance (a config-editable "section"); a fixed
  output contract is always appended so parsing never breaks. See §3, step 4.
- **`orb_enabled`** — static decision (not DeepSeek): run ORB once at the end if true.
- **`analyzers[]`** — `mandatory` ones are always called; the rest are DeepSeek-selected.
  `discover_url` empty ⇒ derived as `api_url + "/discover"`.

**To add an analyzer to a tool, or add a whole new tool, you edit this file (or the GUI) — no
orchestrator code changes.**

---

## 3. The pipeline, step by step

`orchestrator/pipeline.py::run(tool_name, file_bytes, filename, question, job_id)` → `str`.
Every step emits a job event (CLI log + GUI). The whole function is wrapped so a bug never
escapes as a raw MCP error; it always returns a string.

1. **VFR** (`vfr.py`) — *vetting & flow routing*. **Passthrough today**: takes the file, returns the
   same file, reports `"Correct analyzer match"`. It is the seam where real routing logic goes
   later (see §7). Kept as its own step so the flow/telemetry already has the node.

2. **TOOL_ENABLEMENT** (`tool_enablement.py`) — load this tool's `ToolConfig` (cached; the GUI
   can hot-reload it). Split analyzers into `mandatory()` and `optional()`. If the tool has no
   config/analyzers, return a friendly message.

3. **DISCOVER TOOLS** (`discovery.py`) — `GET <base>/discover` for **every** configured analyzer,
   **concurrently**, fail-soft. An analyzer that doesn't answer is dropped (logged) and simply
   not used this run. Tolerates an `ApiResponse` envelope (`data:{…}`).

4. **DEEPSEEK DECISION** (`decide.py`) — choose which **optional** analyzers to run.
   - **Skip DeepSeek entirely** when there are **0 optional** analyzers that discovered OK — the
     answer is fully determined (mandatory only). ORB is orthogonal and never affects this.
     Examples: *1 mandatory + ORB on* → skip. *2 mandatory + ORB off* → skip.
     *1 mandatory + 2 optional* → **DeepSeek runs**.
   - Otherwise, build the prompt: the **per-tool `routing_system_prompt`** (or a generic default)
     **+ a fixed JSON output contract**, then each optional analyzer's `when_to_use`, then the
     question. DeepSeek returns `{"call":[ids], "reason":"…"}`.
   - **Fail-safe:** DeepSeek unreachable or unparseable ⇒ include **all** optional analyzers
     (better to over-analyze than to silently drop a relevant one) and say so in the reason.
   - Mandatory are always kept; result is `Decision(selected_ids, used_deepseek, reason,
     system_prompt, deepseek_raw)`.

5. **CALL analyzers** — the selected analyzers run **concurrently** via `asyncio.gather`, each
   through the single generic `analyzer_client.call(ref, discovery, file, filename, question)`
   which builds the multipart request from the discovery's `params`, POSTs, and validates the
   response to `AnalyzerResult`. It **never raises** — on failure it returns `ok=False` with an
   error result, so one slow/broken analyzer can't sink the others.

6. **CONCATENATE** — join every non-empty `report_markdown` with a horizontal rule
   (`\n\n---\n\n`). The orchestrator never re-formats an analyzer's text.

7. **ORB** (`orb.py`) — if `orb_enabled`, ask the ORB troubleshooting API **once** with the
   combined report + the user's question, and append the answer as `## ORB Suggestions`.
   **Fail-open:** if ORB errors or is empty, the report is returned without it.

8. **DONE** — return the combined markdown. `jobs.finish()` records totals.

---

## 4. Per-tool DeepSeek routing prompt (why it exists)

Different MCP tools route differently. A log tool weighs "does this question need the performance
or EoS companion?"; a future config-generation tool would weigh entirely different companions.
So the **routing system prompt is per tool**, stored in TOOL_ENABLEMENT next to the analyzers,
and editable in the GUI. `decide.py` composes:

```
   <tool's routing_system_prompt   (or the generic default if unset)>
   +
   <fixed OUTPUT CONTRACT: 'return only {"call":[…],"reason":"…"}'>   ← machinery, never edited
```

This lets a tool author rewrite the *guidance* freely — to justify and match that tool's
description — without ever breaking the decision parsing. The exact system prompt used is stored
on the `Decision` and surfaced on the `decide` node in the GUI.

---

## 5. Concurrency model

- **Across tool calls:** every call gets its own `job_id`, its own async context, and its own
  entry in the job registry. The server (uvicorn/Starlette under FastMCP) handles many clients
  and files at once. Verified with 3 simultaneous clients routing three different ways.
- **Within a call:** discovery is concurrent; the selected analyzers are concurrent
  (`asyncio.gather`); ORB is a single call at the end. Wall-clock ≈ slowest analyzer + ORB, not
  the sum.
- The job registry uses a lock and is bounded (30 most-recent jobs; older evicted — durable history/metrics belong in a DB + Grafana later).

---

## 6. Observability — CLI logs, jobs, and the GUI

- **`orchestrator/jobs.py`** is the single source of truth. `emit()` does two things at once:
  writes a structured line to the terminal (via `vlog`, extensive per-step logging with icons,
  timings, and details) **and** appends a `JobEvent` to the in-memory `Job`.
- **`orchestrator/gui.py`** attaches an operator console to the same process via FastMCP custom
  routes (so it reads the **live** in-memory registry — no polling a file). Mounted under **both
  `/gui` and `/mcp/gui`** (the latter so it's reachable behind the prod proxy that forwards only
  `/mcp/*`); the page derives its API base from its own URL, so it works at either prefix:
  - `GET /gui` (and `/mcp/gui`) — the single-page app (`gui/index.html`)
  - `GET …/gui/api/state` — tools (from config) + recent job summaries (last 30, polled ~1.5s)
  - `GET …/gui/api/jobs/{id}` — one job's derived **flow** (ordered nodes + live status/timings)
  - `GET|POST …/gui/api/config` — read / **save** TOOL_ENABLEMENT (validated + hot-reloaded)
- The GUI is **n8n-style**: nodes `INTAKE (tool call + fetched file) → VFR → TOOL_ENABLEMENT →
  DISCOVER → DEEPSEEK → [analyzers in parallel] → CONCAT → ORB → DONE`, colored by status, edges
  animated while running. The flow is **derived per tool/job**. Tabs toggle between tools; the
  sidebar lists the last-30 live jobs; **Integrations** opens a dashboard of every tool + its
  analyzers; the config editor **adds/removes whole tools**, analyzers, ORB, descriptions, and
  each tool's routing prompt; a **dark-mode toggle** persists per browser. (Red/white theme.)
- **Dynamic tools:** `server.py` registers one generic MCP tool per config entry and reconciles
  the live tool set via `sync_tools()` at startup and on every save (through
  `tool_enablement.on_change`). So **adding a tool in the GUI exposes a real MCP tool — name +
  endpoints — with no code and no restart**; removing one unregisters it. All VISTA tools share
  the `source_url`+`question` surface, so one function serves every tool.

> **Security note:** the `/gui` routes are **not** behind the MCP bearer token — it's a local
> operator console. Bind to localhost/a trusted network, or front it with your own auth. The
> config POST is the only mutating route and it validates before writing.

---

## 7. Security posture (unchanged from before the rearchitecture)

- **Auth:** MCP protocol endpoint requires a static bearer token (`StaticTokenVerifier`). The
  server refuses to start with an empty/default token unless `MCP_ALLOW_INSECURE=1`.
- **SSRF guard:** the injected `source_url` is fetched server-side; non-public targets are
  rejected on the initial request and on every redirect hop.
- **Streaming + capped fetch:** logs are streamed and size-capped (`MAX_LOG_BYTES`); signed query
  strings are never logged.
- **TLS:** verified by default; opt-in relaxation is host-scoped (`tlsconf.py`).

---

## 8. File map

```
server.py                     MCP server: auth, log fetch (SSRF/TLS), one thin tool fn → pipeline; mounts GUI
config/tool_enablement.json   TOOL_ENABLEMENT — per-tool: description, routing_system_prompt, orb, analyzers[]
orchestrator/
  models.py                   AnalyzerDiscovery/AnalyzerResult (contract) + ToolConfig/AnalyzerRef/Decision/Job
  tool_enablement.py          load/get/reload/save the config (cached, safe default)
  vfr.py                      VFR — passthrough today (real routing seam; see below)
  discovery.py                GET /discover for all analyzers, concurrent, fail-soft
  deepseek.py                 AI-MCP gateway call (ds4 → ai-mcp fallback) + JSON extraction
  decide.py                   the routing decision + skip-logic + per-tool prompt composition
  analyzer_client.py          the ONE generic multipart call → AnalyzerResult (never raises)
  orb.py                      ORB ask (fail-open) → "## ORB Suggestions"
  pipeline.py                 the whole flow, wired together, always returns a string
  jobs.py                     job/flow registry → CLI logs + GUI
  gui.py                      /gui routes (state, per-job flow, config editor)
gui/index.html                the single-page n8n-style flow GUI (red/white)
fakes/fake_analyzer.py        a real analyzer following the contract, for local testing
docs/                         analyzer_api.md · Architecture.md · how_to_add_analyzer.md · testing.md
```

---

## 9. Next improvements

- **Real VFR logic (highest priority).** `vfr.py` is a passthrough today (returns the file and
  `"Correct analyzer match"`). The seam is already in the flow, the GUI, and the telemetry, so
  the real implementation is a drop-in. Intended responsibilities for the real VFR:
  - **Pre-flight file inspection / normalization** — sniff format, decompress, size/shape checks,
    redact obvious secrets before anything leaves the process.
  - **Analyzer-match gating** — confirm the file actually suits this tool's analyzers and return
    a real match verdict (today it always says "Correct analyzer match"). A negative verdict
    could short-circuit with a helpful message instead of calling analyzers.
  - **Routing hints** — emit structured hints (detected log/event type, product, version) that
    `decide.py` can feed to DeepSeek to sharpen optional-analyzer selection.
  - Keep it fail-safe and cheap; it runs on every call before any analyzer.
- **Discovery cache** with TTL so `/discover` isn't hit on every call.
- **Auth on the GUI** (token or SSO) for non-local deployments; audit log for config edits.
- **Per-analyzer streaming/partial results** to the GUI as they complete.
- **DeepSeek decision cache** keyed by (tool, optional-set, normalized question).
- **Config schema validation** surfaced inline in the GUI editor (field-level errors).
