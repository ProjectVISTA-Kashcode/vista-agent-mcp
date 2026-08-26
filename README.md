# VISTA-MCP

An **MCP (Model Context Protocol) server** that exposes a **growing catalog of Fortinet VISTA
tools** to a partner team's agents, for FortiOS / FortiGate engineering and support. Log analysis
& visualization is the **first** tool; the catalog will grow to include things like config/script
generation, debug-command helpers, End-of-Support & lifecycle lookups, and more. The tools are
**independent and varied** — each has its own inputs and its own text output; **not all take a log
file or return a visualization**. The agent reads each tool's description in `list_tools` and calls
the one whose purpose matches the task.

**Today's tool** — `Log_Analyzer_Visualizer`, backed by the **Log Visualizer "AgentAssist" API**
(FortiGate SD-WAN event logs). The client still sends a `source_url` (+ optional `question`) and
gets back **one text report** — no client-visible change.

> **Architecture (current):** each MCP tool is a thin entry point into a **config-driven
> orchestrator**. On every call it discovers a set of standard **analyzers**, and an
> **AI Controller** decides which optional ones fit the question **and how to call each one from
> the contract it just discovered**; they run **in parallel**, their reports are concatenated, and
> **ORB** troubleshooting is appended — with **no per-analyzer code**. Adding capability is a
> config edit. Every job is stored in Postgres. Full details:
> - [docs/Architecture.md](docs/Architecture.md) — the whole design and flow
> - [docs/analyzer_api.md](docs/analyzer_api.md) — the standard analyzer contract every analyzer follows
> - [docs/how_to_add_analyzer.md](docs/how_to_add_analyzer.md) — add an analyzer or a whole new tool (config, no code)
> - [docs/gui_guide.md](docs/gui_guide.md) — the operator console: Flow · Dashboard · History
> - [docs/db_setup.md](docs/db_setup.md) — durable job history (the prod `psql` commands)
> - [docs/testing.md](docs/testing.md) — run & test locally, with commands
> - **Console** at **`/gui`** — live flow of every job, dashboard analytics, searchable history,
>   and the TOOL_ENABLEMENT / routing-prompt editor. It also serves two standalone pages:
>   **`/gui/user-guide`** (how to drive the console) and **`/gui/api-integration-guide`**
>   (the API an analyzer must expose to plug in) — both also under `/mcp/gui/…`
> - [changes.md](changes.md) — what changed in the latest revision, and why
>
> *(The older `analyzers/` module and `docs/ADDING_ANALYZERS.md` describe the superseded
> one-subclass-per-tool proxy pattern; the orchestrator replaced it.)*

> Production URL: `https://vista.fortinet.com/mcp/` (the served MCP path is **`/mcp/`**).

---

## 1. How it works

```
Partner agent                     VISTA-MCP (this repo)                    Backend analyzer
─────────────                     ─────────────────────                    ────────────────
list_tools ───────────────────►  return tool name/description/schema
                                  (the model decides whether to call)

call Log_Analyzer_Visualizer
  { source_url:<signed URL>,      1. fetch_log(source_url)  ── stream, ≤50MB ──►  (partner's
    question:"..." }              2. LogVAnalyzer.analyze(bytes, question)         signed URL)
                                     ├─ question?  POST .../filter_analyze_visualyze_logs ──►  Log
                                     └─ no q?      POST .../analyze_visualise_logs        ──►  Visualizer
                                  3. ask ORB for troubleshooting steps  ──────────────────►  ORB API
                                  4. fold JSON + ORB → ONE text report  ◄──── {filter, analysis, viz}
        ◄──────────────────────── return report text
```

The report the agent receives concatenates, in order:

1. **Filter summary** — `filter: matched 2495/29169 · queries 1` + how the logs were selected
   (the internal engine name is scrubbed).
2. **Open in Log Visualizer** — a shareable link to the parsed logs.
3. **Analysis** — the AI report (written for a Fortinet TAC engineer).
4. **ORB Suggestions** — troubleshooting steps directly relevant to the analysis. This layer sends
   the analysis + a fixed troubleshoot instruction + **the user's own question (when they asked one)**
   to the ORB "ask" API, so the remediation targets what was actually asked, and inserts ORB's answer
   here (between analysis and the iframe).
   **Fail-open**: if ORB is disabled, unreachable, slow, or there's no real analysis, this section is
   simply omitted and the rest of the report is unaffected.
5. **Interactive Visualization** — an `iframe` URL the agent is told to embed in its answer.

If a question is given, the filter block appears (filter → analyze → visualize). **With no question**,
the Filter block is omitted (just analyze → visualize). ORB Suggestions appear whenever there's a real
analysis to troubleshoot. If the uploaded log isn't a supported type, the tool returns a clear
"not supported yet" message instead.

### The one special input: the log URL
Per the partner contract, **we never ask the user for the file** — the platform injects a
short-lived **signed URL** into a field we declare (`source_url`). The tool **fetches it
promptly** (it expires in minutes), **streams** it (logs can be large; capped by
`MAX_LOG_BYTES`), and **does not store** it. The model fills only `question`.

---

## 2. Repository layout

```
VISTA-MCP/
├── server.py                    # FastMCP server: /mcp route, bearer auth, log fetch → orchestrator; mounts /gui
├── config/tool_enablement.json  # TOOL_ENABLEMENT — per-tool: description, routing_system_prompt, orb, analyzers[]
├── orchestrator/                # the config-driven core (no per-analyzer code)
│   ├── models.py                #   the standard contract + ToolConfig/AnalyzerRef/CallPlan/Decision/Job
│   ├── tool_enablement.py       #   load/save the per-tool config (cached, hot-reload)
│   ├── vfr.py                   #   VFR — passthrough today (real routing seam; see Architecture.md §11)
│   ├── discovery.py             #   GET /discover for all analyzers (concurrent, fail-soft) + probe()
│   ├── ai_controller.py         #   the AI gateway client (fail-safe)
│   ├── decide.py · plan.py      #   ALWAYS-ON decision: pick optional analyzers + plan every call
│   │                            #   from live discovery, then validate the plan against it
│   ├── analyzer_client.py       #   the ONE generic call — executes a validated plan
│   ├── orb.py · pipeline.py     #   ORB (fail-open) + the whole flow wired together
│   ├── jobs.py · db.py          #   job/flow registry (CLI logs) + durable history in Postgres
│   └── gui.py                   #   the /gui routes (state, history, analytics, config, probe)
├── gui/index.html               # the operator console — Flow · Dashboard · History (red/white, dark mode)
├── gui/user_guide.html          # served at /gui/user-guide            (console walkthrough)
├── gui/api_guide.html           # served at /gui/api-integration-guide (analyzer contract)
├── fakes/fake_analyzer.py       # a reference analyzer following the contract (+ run_fakes.sh)
├── fakes/fake_analyzer_v2.py    # the same contract with a DIFFERENT request shape (dynamic-discovery test)
├── client_test.py               # end-to-end MCP client (serves a log, list_tools, call_tool)
├── test_data/                   # sample SD-WAN logs
├── docs/                        # Architecture.md · analyzer_api.md · how_to_add_analyzer.md ·
│                                # gui_guide.md · db_setup.md (+ .sql) · testing.md
├── analyzers/                   # SUPERSEDED pre-orchestrator subclass pattern (kept for reference)
└── starter_kit/                 # the partner team's original example (reference, untouched)
```

---

## 3. Run it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# point at a running Log Visualizer AgentAssist backend (see the LogAssist repo)
export LOGV_API_BASE="http://127.0.0.1:8802/logVisualizer/api/agent_assist"
export LOGV_VIEW_BASE="https://vista.fortinet.com/logVisualizer"   # link/iframe base
export MCP_AUTH_TOKEN="a-strong-shared-token"                       # partner sends this
# for LOCAL testing only, because the test client serves the log on 127.0.0.1:
export MCP_ALLOW_PRIVATE_FETCH=1

python server.py            # → http://0.0.0.0:8100/mcp/
```

### Test it end-to-end (no partner infra needed)
In another shell (server running):

```sh
. .venv/bin/activate
export MCP_AUTH_TOKEN="a-strong-shared-token"        # same token the server expects

python client_test.py --list-only                    # inspect the tool schema
python client_test.py --file test_data/sdwan-small.log --question "SLA failures for Austin"
python client_test.py --file test_data/sdwan-small.log --no-question      # analyze + visualize
python client_test.py --file test_data/DEMO_LOG_VISUALIZER_SDWAN.log --question "packet loss over 50%"
```

The client serves the log over a throwaway local HTTP server (standing in for the partner's
signed URL), connects over MCP with the bearer token, and prints the exact text report the
agent would receive. *(If the MCP server runs on another host, pass `--host-ip <ip the server
can reach the client at>`.)*

---

## 4. Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | bind host |
| `MCP_PORT` | `8100` | bind port (served path is `/mcp/`) |
| `MCP_AUTH_TOKEN` | `vista-dev-token` | **shared bearer token** the partner must send. The server **refuses to start** with an empty/default token unless `MCP_ALLOW_INSECURE=1`. **Set a strong value in prod.** |
| `MCP_ALLOW_INSECURE` | _(off)_ | `1` permits the dev/empty token (local dev only) |
| `MCP_ALLOW_PRIVATE_FETCH` | _(off)_ | `1` allows fetching private/loopback URLs (needed for the local test client; prod signed URLs are public) |
| `MAX_LOG_BYTES` | `52428800` (50 MB) | streaming fetch cap for the injected log |
| `MCP_FETCH_CA_BUNDLE` | _(unset)_ | PEM bundle of extra CAs to trust on outbound calls (internal PKI). Bad path ⇒ the server refuses to start |
| `MCP_FETCH_INSECURE_TLS_HOSTS` | _(empty)_ | comma-separated hosts to skip TLS verification for — a scoped `curl -k`, for hosts whose cert name doesn't match |
| `MCP_FETCH_INSECURE_TLS` | _(off)_ | `1` skips TLS verification for **every** host (blunt; prefer the host list) |
| `MCP_LOG_LEVEL` | `INFO` | terminal flow-log verbosity (`DEBUG` previews the report text) |
| `LOGV_API_BASE` | `http://127.0.0.1:8802/logVisualizer/api/agent_assist` | Log Visualizer AgentAssist base |
| `LOGV_VIEW_BASE` | `https://vista.fortinet.com/logVisualizer` | SPA base for the returned session links/iframe |
| `ORB_ENABLED` | `1` | set `0` to skip the ORB troubleshooting step (report is built without it) |
| `ORB_ASK_URL` | `http://172.17.96.58:9345/orb/api/ask` | ORB "ask" API queried after the analysis for troubleshooting steps |
| `ORB_USERNAME` | `logV_mcp_call` | `username` sent to ORB |
| `ORB_TIMEOUT` | `180` | ORB request timeout (s) — ORB is deep-research and slow (~45–140 s); fail-open on timeout |
| `AI_CONTROLLER_ENABLED` | `1` | `0` turns **every** AI step off — routing and every request fall back to the deterministic, discovery-built policy |
| `AGENTASSIST_BASE_URL` | `https://agentassist.corp.fortinet.com/v1` | OpenAI-compatible gateway the Pydantic AI agents run against |
| `AGENTASSIST_API_KEY` | _(unset)_ | **required for any AI step to run.** Unset ⇒ the controller reports `OFF — AGENTASSIST_API_KEY is not set` and everything takes the deterministic path |
| `AI_MODEL_FAST` | `model-fast` | tier used for routing + invocation planning (every job) |
| `AI_MODEL_PRO` | `model-pro` | tier used to draft a tool config from a discovery URL |
| `AI_MODEL_LONG` | `model-long` | tier used to render a large analyzer result into markdown |
| `AI_TIMEOUT` | `60` | per-request timeout (s) |
| `AI_TEMPERATURE` | `0` | these are decisions, not prose |
| `AI_REPORT_RENDER` | `auto` | rendering of results with no `report_markdown`: `auto` = deterministic, AI only when thin · `1` = always AI · `0` = never |
| `MAX_FILE_TEXT_CHARS` | `400000` | cap on file text substituted into a body param via `{{file_text}}` |
| `DATABASE_URL` | _(unset)_ | Postgres for durable job history + console analytics, e.g. `postgresql+psycopg://user:pass@host:5432/usage_logs`. Unset ⇒ history off, everything else unchanged. See [docs/db_setup.md](docs/db_setup.md) |
| `MCP_DB_AUTO_CREATE` | `1` | `0` skips `CREATE TABLE IF NOT EXISTS` (prod: create the schema by hand) |
| `MCP_DB_STORE_REPORTS` | `1` | `0` stores report lengths but not the text |
| `MCP_JOBS_MEMORY` | `60` | how many recent jobs the live console keeps in memory |

Every tool call logs the full flow to the terminal with a correlation id:

```
[633d5f5b] ▶ TOOL CALL  Log_Analyzer_Visualizer  question='SLA failures for LO_C1_2 and High CPU'  source_url=http://…/sdwan.log
[633d5f5b] fetch: done — 13,885,720 bytes, filename='DEMO_LOG_VISUALIZER_SDWAN.log' in 0.31s
[633d5f5b]   ✔ [tool_enablement] ok mandatory=['logv']  optional=['perf']  orb=on
[633d5f5b]   ✔ [discover] ok discovered=['logv', 'perf']
[633d5f5b]   → [ai_controller] asking (4,068-char prompt · mode=select+plan · mandatory=['logv'] optional=['perf'])
[633d5f5b]   ✔ [ai_controller] ok (21022ms) The user asks about 'High CPU', which is a performance concern…
[633d5f5b]   → call[logv] POST …/agent_assist/run  (file=13,885,720B → file, fields=['question'], plan=ai)
[633d5f5b]   → call[perf] POST …/perf/run          (file=13,885,720B → file, fields=['question'], plan=ai)
[633d5f5b]   ✔ [concat] ok 2 section(s), 7,568 chars
[633d5f5b]   ✔ [orb] ok (38214ms) 11,159 chars appended
[633d5f5b] ◀ JOB DONE  report=18,749 chars in 86.2s
```

---

## 5. Auth

The server uses FastMCP's `StaticTokenVerifier` — a **pre-shared bearer token**. The partner
agent must send `Authorization: Bearer <MCP_AUTH_TOKEN>`; anything else is **401 Unauthorized**
(verified). Rotate by changing `MCP_AUTH_TOKEN` and re-sharing.

**Fail-closed:** the server **refuses to start** if `MCP_AUTH_TOKEN` is empty or the built-in
`vista-dev-token`, unless `MCP_ALLOW_INSECURE=1` (local dev only) — so you can't accidentally
ship it open or with the public default.

**SSRF guard:** the injected `source_url` is fetched server-side, so before fetching (and on
every redirect hop) the host is resolved and **rejected if it's a private / loopback /
link-local / reserved address** (e.g. cloud metadata, the internal LogV backend). Production
signed URLs are public, so this is transparent; for the **local test client** (which serves the
log on `127.0.0.1`) set `MCP_ALLOW_PRIVATE_FETCH=1`. Signed URLs are never logged (the query
string is redacted) and are never echoed back in error messages.

### TLS trust on outbound calls

Every outbound call (log fetch, LogV forward, ORB ask) **verifies TLS**, exactly like `curl`
without `-k`. A rejected certificate fails as `httpx.ConnectError` in ~0.1 s — before any HTTP
request is sent — and the flow log names it:

```
✖ fetch failed (ConnectError, TLS certificate rejected) for https://sa-staging.corp.fortinet.com/…
  ↳ TLS certificate rejected by 'sa-staging.corp.fortinet.com' (same failure as `curl` without `-k`). Fix one of: …
```

Two ways to accommodate an internal host, in order of preference:

| Symptom (`curl` says…) | Fix |
|---|---|
| `unable to get local issuer certificate` — signed by an internal CA | `MCP_FETCH_CA_BUNDLE=/etc/ssl/certs/corp-ca.pem` (mount the PEM into the container) |
| `no alternative certificate subject name matches target host name` — name/SAN mismatch. **No CA bundle can fix this** | `MCP_FETCH_INSECURE_TLS_HOSTS=sa-staging.corp.fortinet.com` |

`MCP_FETCH_INSECURE_TLS_HOSTS` is a *scoped* `curl -k`: verification is skipped for those exact
hostnames only, every other host stays fully verified, and each use logs a `WARNING`. A relaxed
request may also only be **redirected to another relaxed host** — so an unverified hop can't
hand the connection off to an arbitrary target. `MCP_FETCH_INSECURE_TLS=1` relaxes everything
and should be a last resort.

---

## 6. Going live (what to send the partner)

Per the starter kit, once the server is reachable from their backend:

1. **Server URL** — `https://vista.fortinet.com/mcp/` (include the `/mcp/` path).
2. **Auth token** — the `MCP_AUTH_TOKEN` value (share securely).
3. **For each log-consuming tool** — the tool **name** and the **input field** that receives the
   injected log URL:
   - `Log_Analyzer_Visualizer` → **`source_url`**

They register the server, wire `source_url` to their signed-URL injector, and enable the tool.

---

## 7. Design notes

- **Proxy, not a re-implementation.** VISTA-MCP holds no analysis logic; it forwards to the
  analyzer's API and formats the reply. The Log Visualizer decides the log subtype (SD-WAN
  today) and how to handle it — VISTA-MCP just surfaces the result (including "not supported").
- **Pluggable analyzers, discovered live.** The contract is
  [docs/analyzer_api.md](docs/analyzer_api.md): `GET <base>/discover` + `POST <query.path>`. The
  orchestrator holds one discover function, one call function and one read path, and learns each
  analyzer's request shape from its discovery **on every call** — so an analyzer can change its
  API without a change here. Adding one is a config edit in the console.
- **The model can propose, never decide alone.** The AI Controller's request plan is validated
  field-by-field against the analyzer's own discovery before anything is sent; anything not
  advertised is corrected or dropped, and the correction is recorded.
- **Fail-safe everywhere.** No AI gateway ⇒ deterministic routing + discovery-built requests.
  No ORB ⇒ report without it. No database ⇒ live view only. One analyzer down ⇒ the others
  still answer.
- **Bounded output.** The report is text; the analysis is already reduced by the backend.
- **Stateless per call for the log.** Fetch → forward → return; the log itself is never stored.
  Job *metadata* and reports are persisted for the console's history and analytics.
- **Framework.** FastMCP (runs on an ASGI/Starlette app via uvicorn) implements the MCP
  protocol and the `/mcp/` HTTP transport — the same framework the partner's starter kit uses.

## 8. Verified behavior (end-to-end)

| Scenario | Result |
|---|---|
| `list_tools` | every configured tool (`Log_Analyzer_Visualizer` today), inputs `source_url` (required) + `question` |
| With question (SD-WAN) | filter block (matched N/total) + LogV link + analysis + **ORB Suggestions** + iframe URL |
| No question (SD-WAN) | LogV link + analysis + **ORB Suggestions** + iframe URL (no filter block) |
| ORB Suggestions section | troubleshooting steps directly relevant to the findings, **added by this MCP layer between the analysis and the visualization** (fail-open) |
| Response time | ~1–2 min (full analysis + ORB troubleshooting research); within the 300 s read timeout |
| Non-SD-WAN log | "classified as `Traffic:forward`, not supported yet…" |
| 14 MB log | streamed + processed (29,169 logs) |
| Internal engine name | scrubbed (0 mentions in output) |
| Wrong/missing bearer token | **401 Unauthorized** |
| AI Controller | runs on **every** job — `select+plan` with optional analyzers, `plan-only` without; never skipped |
| Analyzer with a different contract | called correctly with no code change (renamed file/question params, an extra required param, a different path) |
| AI gateway unreachable | fail-safe: all optional analyzers run, requests built from discovery, client output unaffected |
| Job history | every job + step + per-analyzer request/result + final report in Postgres, replayable in the console after a restart |
