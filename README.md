# VISTA-MCP

An **MCP (Model Context Protocol) server** that exposes a **growing catalog of Fortinet VISTA
tools** to a partner team's agents, for FortiOS / FortiGate engineering and support. Log analysis
& visualization is the **first** tool; the catalog will grow to include things like config/script
generation, debug-command helpers, End-of-Support & lifecycle lookups, and more. The tools are
**independent and varied** — each has its own inputs and its own text output; **not all take a log
file or return a visualization**. The agent reads each tool's description in `list_tools` and calls
the one whose purpose matches the task.

**Today's tool** — `Log_Analyzer_Visualizer`, backed by the **Log Visualizer "AgentAssist" API**
(FortiGate SD-WAN event logs). It follows a **proxy** pattern: fetch the platform-injected log,
forward it (plus the user's question) to that tool's backend, and return **one text report**.
Backend-backed tools like this share the MCP protocol, auth, log-fetch, and SSRF plumbing; tools
that don't proxy a backend are added as plain MCP tool functions
(see [docs/ADDING_ANALYZERS.md](docs/ADDING_ANALYZERS.md)).

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
├── server.py                 # FastMCP server: /mcp route, bearer auth, log fetch, tool registration
├── analyzers/
│   ├── base.py               # Analyzer abstraction (one class == one MCP tool)
│   └── logv.py               # LogVAnalyzer: forwards to AgentAssist, folds JSON → one text report
├── client_test.py            # end-to-end MCP client (serves a log, list_tools, call_tool)
├── test_data/                # sample SD-WAN logs (copied from the Log Visualizer repo)
├── requirements.txt          # fastmcp, httpx, pydantic
├── .env.example              # configuration reference
├── docs/ADDING_ANALYZERS.md  # how to add a new VISTA analyzer as a tool
└── starter_kit/              # the partner team's original example (reference, untouched)
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

Every tool call logs the full flow to the terminal with a correlation id:

```
[110877fa] ▶ TOOL CALL  Log_Analyzer_Visualizer  question='SLA failures on Austin'  source_url=http://…/sdwan.log
[110877fa] fetch: GET http://…/sdwan.log (streaming, cap 52,428,800B)
[110877fa] fetch: done — 577,618 bytes, filename='sdwan-small.log' in 0.02s
[110877fa] forward → LogV: POST …/filter_analyze_visualyze_logs  (question=yes, file=577,618B, spa_base=…)
[110877fa] forward: LogV responded HTTP 200 in 17.32s
[110877fa] LogV result: log_type=System:sdwan  filter.matched=50/948  analysis=4,316chars  session_id=21b2ece7-…  iframe=yes
[110877fa] ◀ TOOL CALL done — returning 5,177-char report in 17.34s
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
- **Pluggable analyzers.** `analyzers/base.Analyzer` is the contract: `name`, `log_field`,
  `description()`, `analyze(log_bytes, filename, question) -> str`. One subclass per backend,
  one tool per subclass. See [docs/ADDING_ANALYZERS.md](docs/ADDING_ANALYZERS.md).
- **Bounded output.** The report is text; the analysis is already reduced by the backend.
- **Stateless per call.** Fetch → forward → return; nothing is stored.
- **Framework.** FastMCP (runs on an ASGI/Starlette app via uvicorn) implements the MCP
  protocol and the `/mcp/` HTTP transport — the same framework the partner's starter kit uses.

## 8. Verified behavior (end-to-end)

| Scenario | Result |
|---|---|
| `list_tools` | 1 tool `Log_Analyzer_Visualizer`, inputs `source_url` (required) + `question` |
| With question (SD-WAN) | filter block (matched N/total) + LogV link + analysis + **ORB Suggestions** + iframe URL |
| No question (SD-WAN) | LogV link + analysis + **ORB Suggestions** + iframe URL (no filter block) |
| ORB Suggestions section | troubleshooting steps directly relevant to the findings, **added by this MCP layer between the analysis and the visualization** (fail-open) |
| Response time | ~1–2 min (full analysis + ORB troubleshooting research); within the 300 s read timeout |
| Non-SD-WAN log | "classified as `Traffic:forward`, not supported yet…" |
| 14 MB log | streamed + processed (29,169 logs) |
| Internal engine name | scrubbed (0 mentions in output) |
| Wrong/missing bearer token | **401 Unauthorized** |
