# Running & testing VISTA-MCP locally

Everything below runs on one box: the LogV analyzer, the fake analyzers, Postgres, the MCP server,
and the client. This mirrors production (client → MCP → analyzers + AI Controller + ORB + DB) but
locally.

Two virtualenvs are involved:

| venv | has | used for |
|---|---|---|
| **VISTA-MCP** `/home/jaskirat/VISTA-MCP/.venv` | fastmcp, httpx, pydantic, psycopg | the MCP server + client |
| **LogV** `/home/jaskirat/log_visio/LogAssist/backend/.venv` | fastapi, uvicorn, python-multipart | the LogV backend + the **fakes** (FastAPI) |

Ports: **8802** LogV · **8811** perf (fake) · **8812** eos (fake) · **8813** perf2 (fake, contract
v2) · **8100** MCP server (+ `/gui`) · **5432** Postgres.

---

## 1. Start the analyzers

**LogV (the mandatory analyzer) — run the REAL backend so it does REAL AI analysis.** This is the
one that matters: LogV's `/run` endpoint runs the genuine *filter → AI analyze → visualize*
pipeline. Its AI call hits `https://vista.fortinet.com/ai-mcp/ds4/generate` — reachable from here.

```bash
cd /home/jaskirat/log_visio/LogAssist/backend
.venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8802 --log-level warning
```

You should see real analysis in its log: `filter_logs: DONE matched=…`, `Sending prompt to AI`,
`DeepSeek returned N chars`. The `## Analysis` section of the report is then the full AI SD-WAN
report (Executive Summary / Findings / Recommendations).

> ⚠️ **Do NOT use the AI-stub mock for real testing.** There is an offline harness that
> monkeypatches `src.AI_analyzer.query_ai` to return a fixed one-liner (used only for fast,
> network-free pipeline plumbing tests). If LogV's `## Analysis` is a single canned sentence, you
> are running the stub — start the real backend above instead.

**The fake analyzers** — they follow the standard contract and share no code with the orchestrator:

```bash
cd /home/jaskirat/VISTA-MCP
./fakes/run_fakes.sh          # perf :8811 · eos :8812 · perf2 :8813  (uses the LogV venv)
# ./fakes/run_fakes.sh stop   # to stop them
```

`perf` and `eos` use the familiar `file` + `question` shape. **`perf2` deliberately does not** —
its file param is `payload`, its question param is `query` (required), it has an extra required
`mode` param (with a `default`), and its endpoint is `/perf2/v2/analyze`. It exists to prove the
orchestrator adapts to a changed contract with no code change (§4).

Verify all four answer discovery:

```bash
curl -s http://127.0.0.1:8802/logVisualizer/api/agent_assist/discover | head -c 200; echo
curl -s http://127.0.0.1:8811/perf/discover  | head -c 200; echo
curl -s http://127.0.0.1:8812/eos/discover   | head -c 200; echo
curl -s http://127.0.0.1:8813/perf2/discover | head -c 200; echo
```

---

## 2. Start the MCP server (with the console + history)

```bash
cd /home/jaskirat/VISTA-MCP
MCP_HOST=0.0.0.0 MCP_PORT=8100 \
MCP_AUTH_TOKEN='vista-test-strong-xyz' \
MCP_ALLOW_PRIVATE_FETCH=1 \
DATABASE_URL='postgresql+psycopg://jaskirat:jaskirat@localhost:5432/usage_logs' \
.venv/bin/python server.py
```

The banner prints the tools, their analyzers (mandatory/optional), ORB state, the **AI Controller**
endpoint, the **job history** connection, and the console URL:

```
  AI Controller    : ON → https://vista.fortinet.com/ai-mcp/ds4/generate  (timeout 60s)
  job history (DB) : ON → postgresql://jaskirat:***@localhost:5432/usage_logs
```

Open the console at:

```
http://127.0.0.1:8100/gui
```

> `MCP_ALLOW_PRIVATE_FETCH=1` is only needed locally so the server may fetch the client's
> loopback/LAN log URL. In production, signed URLs are public and this stays off.
> `DATABASE_URL` is optional — without it the server runs exactly as before, with the live view
> only (see [`db_setup.md`](db_setup.md)).

---

## 3. Drive it with the client

The client serves a sample log over a throwaway HTTP server (standing in for the platform's signed
URL), then calls the tool over MCP. **The client is unchanged** — this is the same command line
that has always worked:

```bash
cd /home/jaskirat/VISTA-MCP
export MCP_URL="http://127.0.0.1:8100/mcp" MCP_AUTH_TOKEN='vista-test-strong-xyz'

# the canonical end-to-end run
.venv/bin/python client_test.py --file test_data/DEMO_LOG_VISUALIZER_SDWAN.log \
  --question "SLA failures for LO_C1_2 and High CPU "

# (a) NEGATIVE routing — an SLA question → logv only (no companion added)
.venv/bin/python client_test.py --file test_data/sdwan-small.log \
  --question "SLA failures on the Austin healthcheck at 12:07"

# (b) POSITIVE routing — a performance question → the perf analyzer is added
.venv/bin/python client_test.py --file test_data/sdwan-small.log \
  --question "High CPU and conserve mode — what performance problem is this?"

# no question at all → analyze + visualize
.venv/bin/python client_test.py --file test_data/sdwan-small.log --no-question

# just list tools (shows the config-driven description)
.venv/bin/python client_test.py --list-only
```

Watch the server terminal (or the console) — you'll see the whole flow per call:
`intake → vfr → tool_enablement → discover → ai_controller → analyze:… (parallel) → concat → orb
→ done`, with the AI Controller's reason, the planned request for each analyzer, and which
analyzers ran.

### Concurrency (multiple clients/files at once)
```bash
export MCP_URL="http://127.0.0.1:8100/mcp" MCP_AUTH_TOKEN='vista-test-strong-xyz'
.venv/bin/python client_test.py --file test_data/sdwan-small.log --question "SLA failures on Austin healthcheck" &
.venv/bin/python client_test.py --file test_data/sdwan-small.log --question "High CPU and conserve mode?" &
.venv/bin/python client_test.py --file test_data/sdwan-small.log --question "End-of-support date for this model?" &
wait
```
All three run concurrently with independent job ids and independent routing. Open `/gui` to watch
them animate side by side.

---

## 4. Verify the dynamic part (the AI Controller + live discovery)

These are the checks that matter for the "discover and call accordingly" behaviour. They use a
throwaway config so your real `config/tool_enablement.json` is untouched:

```bash
cat > /tmp/dyn_config.json <<'EOF'
{"tools":{
 "Dynamic_Contract_Test":{"description":"mandatory-only","routing_system_prompt":"",
   "orb_enabled":false,
   "analyzers":[{"id":"perf2","title":"Performance Analyzer v2",
     "api_url":"http://127.0.0.1:8813/perf2","discover_url":"","mandatory":true,
     "enabled":true,"timeout":60}]},
 "Mixed_Routing_Test":{"description":"one mandatory + two optional","routing_system_prompt":"",
   "orb_enabled":false,
   "analyzers":[
     {"id":"perf","title":"Performance Analyzer","api_url":"http://127.0.0.1:8811/perf","discover_url":"","mandatory":true,"enabled":true,"timeout":60},
     {"id":"perf2","title":"Performance Analyzer v2","api_url":"http://127.0.0.1:8813/perf2","discover_url":"","mandatory":false,"enabled":true,"timeout":60},
     {"id":"eos","title":"End-of-Support Lookup","api_url":"http://127.0.0.1:8812/eos","discover_url":"","mandatory":false,"enabled":true,"timeout":60}]}}}
EOF

TOOL_ENABLEMENT_PATH=/tmp/dyn_config.json MCP_PORT=8101 \
MCP_AUTH_TOKEN='vista-test-strong-xyz' MCP_ALLOW_PRIVATE_FETCH=1 \
.venv/bin/python server.py
```

| # | Check | How | Expected |
|---|---|---|---|
| 1 | **The AI Controller never skips** | call `Dynamic_Contract_Test` (0 optional analyzers) | the `ai_controller` step is **ok**, not skipped, and logs `mode=plan-only` |
| 2 | **A changed contract is followed** | same call | `→ call[perf2] POST …/perf2/v2/analyze (file → payload, fields=['mode','query'], plan=ai)`, and perf2's report echoes `query` and `mode` |
| 3 | **A param value is reasoned about** | ask a question naming an interface, e.g. *"High CPU on the LO_C1_2 interface"* | `mode='deep'` (the param description says to use `deep` when a specific device/metric is named); a vague question gets `quick` |
| 4 | **Per-analyzer planning in one job** | call `Mixed_Routing_Test` with a CPU question | `perf` is called with `file`+`question`, `perf2` with `payload`+`query`+`mode`, in the same job |
| 5 | **Selection still works both ways** | CPU question vs. end-of-support question | `perf2` added for the first, `eos` for the second — never both |
| 6 | **Hallucinations can't reach an analyzer** | inspect any analyzer node in the console | `plan source: ai` with no notes; if the model had proposed an undeclared field/host/verb it would read `ai-corrected` and list what was replaced |

### Fail-safe (AI gateway down)

```bash
TOOL_ENABLEMENT_PATH=/tmp/dyn_config.json MCP_PORT=8102 \
MCP_AUTH_TOKEN='vista-test-strong-xyz' MCP_ALLOW_PRIVATE_FETCH=1 \
AI_CONTROLLER_GEN_URL=http://127.0.0.1:9/dead AI_CONTROLLER_FALLBACK_URL=http://127.0.0.1:9/dead \
AI_CONTROLLER_TIMEOUT=5 \
.venv/bin/python server.py
```

Expected: the step reports `AI Controller unavailable → fail-safe: running all analyzers with
requests built from discovery`, **every optional analyzer runs**, each request is built from the
discovery document (`plan=deterministic`) — including `payload`/`query` for perf2 and `mode=quick`
from its advertised `default` — and the client still gets a normal report. `AI_CONTROLLER_ENABLED=0`
produces the same deterministic behaviour without even trying the gateway.

---

## 5. Verify the history / console

```bash
# API
curl -s 'http://127.0.0.1:8100/gui/api/state?limit=3'          | python3 -m json.tool | head
curl -s 'http://127.0.0.1:8100/gui/api/jobs?limit=5'           | python3 -m json.tool | head
curl -s 'http://127.0.0.1:8100/gui/api/jobs/<job_id>'          | python3 -m json.tool | head
curl -s 'http://127.0.0.1:8100/gui/api/analytics?since_hours=24' | python3 -m json.tool | head
curl -s 'http://127.0.0.1:8100/gui/api/facets'                 | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8100/gui/api/probe \
  -H 'Content-Type: application/json' -d '{"url":"http://127.0.0.1:8813/perf2"}' | head -c 300

# save config (validates + hot-reloads). Invalid config is rejected without touching the file:
curl -s -X POST http://127.0.0.1:8100/gui/api/config \
  -H 'Content-Type: application/json' --data @config/tool_enablement.json
```

```sql
-- what was stored for the last job
SELECT job_id, tool_name, status, duration_ms, selected_ids, used_ai, ai_mode, ai_plan_source,
       orb_status, report_chars FROM mcp_jobs ORDER BY started_at DESC LIMIT 1;
SELECT seq, step, status, elapsed_ms, left(detail,60) FROM mcp_job_events
 WHERE job_id = (SELECT job_id FROM mcp_jobs ORDER BY started_at DESC LIMIT 1) ORDER BY seq;
SELECT analyzer_id, selected, called, ok, request_url, request_file_param, request_fields,
       plan_source, plan_notes FROM mcp_job_analyzers
 WHERE job_id = (SELECT job_id FROM mcp_jobs ORDER BY started_at DESC LIMIT 1);
```

In the console: **History** shows the job (even one produced by a different server instance),
clicking it replays the flow from the database, and the **AI Controller** node shows the prompt,
the raw answer and every planned call.

---

## 6. What "passing" looks like

- **Client sees no change:** the returned report has the same shape as before — the LogV analysis
  + interactive dashboard link + `## ORB Suggestions` (+ any companion sections), in that order.
- **Routing is correct both ways:** SLA/traffic questions → `logv` only; CPU/conserve questions →
  `logv + perf`; EoS questions → `logv + eos`. The `ai_controller` step logs a one-line reason.
- **The AI Controller always runs:** `mode=select+plan` when there are optional analyzers,
  `mode=plan-only` when there are not. It is never "skipped" unless it is disabled or unavailable.
- **Requests match the contract:** each analyzer is called exactly the way its own `/discover`
  advertised, and `plan source` is `ai` (or `deterministic` on the fail-safe path).
- **Parallelism:** selected analyzers show overlapping timings; total ≈ AI Controller + slowest
  analyzer + ORB.
- **ORB:** appended once as `## ORB Suggestions` when the tool has `orb_enabled` (fail-open).
- **Concurrency:** several jobs run at once with separate ids and correct independent routing.
- **History:** every job is in `mcp_jobs` with its events, per-analyzer rows and final report, and
  is replayable in the console after a restart.

---

## 7. Config knobs (env)

| var | default | meaning |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8100` | bind address |
| `MCP_AUTH_TOKEN` | — (required) | bearer token clients must send; server refuses a weak/default token unless `MCP_ALLOW_INSECURE=1` |
| `MCP_ALLOW_INSECURE` | off | permit a dev/no token (local only) |
| `MCP_ALLOW_PRIVATE_FETCH` | off | allow fetching loopback/LAN log URLs (local testing only) |
| `TOOL_ENABLEMENT_PATH` | `config/tool_enablement.json` | path to the TOOL_ENABLEMENT config |
| `MAX_LOG_BYTES` | `52428800` | streaming fetch cap |
| `MCP_LOG_LEVEL` | `INFO` | `DEBUG` also logs the AI Controller prompt and previews the report |
| `AI_CONTROLLER_ENABLED` | `1` | `0` = deterministic routing + discovery-built requests |
| `AI_CONTROLLER_GEN_URL` / `AI_CONTROLLER_FALLBACK_URL` / `AI_CONTROLLER_TIMEOUT` | see `.env.example` | the AI gateway |
| `DATABASE_URL` | _(unset)_ | durable history; unset ⇒ live view only ([`db_setup.md`](db_setup.md)) |
| `MCP_JOBS_MEMORY` | `60` | live in-memory job window |

*(No pytest suites by design — this is exercised via the CLI flows above and the live console.)*
