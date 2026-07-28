# Running & testing VISTA-MCP locally

Everything below runs on one box: the LogV analyzer, two fake analyzers, the MCP server, and the
client. This mirrors production (client → MCP → analyzers + DeepSeek + ORB) but locally.

Two virtualenvs are involved:

| venv | has | used for |
|---|---|---|
| **VISTA-MCP** `/home/jaskirat/VISTA-MCP/.venv` | fastmcp, httpx, pydantic | the MCP server + client |
| **LogV** `/home/jaskirat/log_visio/LogAssist/backend/.venv` | fastapi, uvicorn, python-multipart | the LogV backend + the **fakes** (FastAPI) |

Ports: **8802** LogV · **8811** perf (fake) · **8812** eos (fake) · **8100** MCP server (+ `/gui`).

---

## 1. Start the analyzers

**LogV (the mandatory analyzer) — run the REAL backend so it does REAL AI analysis.** This is
the one that matters: LogV's `/run` endpoint runs the genuine *filter → AI analyze → visualize*
pipeline (the same engine the old `filter_analyze_visualyze_logs` route used). Its AI call hits
`https://vista.fortinet.com/ai-mcp/ds4/generate` — reachable from here.

```bash
cd /home/jaskirat/log_visio/LogAssist/backend
.venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8802 --log-level warning
```

You should see real analysis in its log: `filter_logs: DONE matched=…`, `Sending prompt to AI
(DeepSeek vista-ds4)`, `DeepSeek returned N chars`. The `## Analysis` section of the report is
then the full AI SD-WAN report (Executive Summary / Findings / Recommendations).

> ⚠️ **Do NOT use the AI-stub mock for real testing.** There is an offline harness that
> monkeypatches `src.AI_analyzer.query_ai` to return a fixed one-liner (used only for fast,
> network-free pipeline plumbing tests). If LogV's `## Analysis` is a single canned sentence,
> you are running the stub — start the real backend above instead.

**Two fake analyzers (perf + eos)** — they follow the standard contract and share no code with
the orchestrator:

```bash
cd /home/jaskirat/VISTA-MCP
./fakes/run_fakes.sh          # starts perf :8811 and eos :8812 (uses the LogV venv for FastAPI)
# ./fakes/run_fakes.sh stop   # to stop them
```

Verify all three answer discovery:

```bash
curl -s http://127.0.0.1:8802/logVisualizer/api/agent_assist/discover | head -c 200; echo
curl -s http://127.0.0.1:8811/perf/discover | head -c 200; echo
curl -s http://127.0.0.1:8812/eos/discover  | head -c 200; echo
```

---

## 2. Start the MCP server (with the GUI)

```bash
cd /home/jaskirat/VISTA-MCP
MCP_HOST=0.0.0.0 MCP_PORT=8100 \
MCP_AUTH_TOKEN='vista-test-strong-xyz' \
MCP_ALLOW_PRIVATE_FETCH=1 \
.venv/bin/python server.py
```

The banner prints the tool, its analyzers (mandatory/optional), ORB state, whether a per-tool
routing prompt is set, and the **GUI URL**. Open the flow GUI at:

```
http://127.0.0.1:8100/gui
```

> `MCP_ALLOW_PRIVATE_FETCH=1` is only needed locally so the server may fetch the client's
> loopback/LAN log URL. In production, signed URLs are public and this stays off.

---

## 3. Drive it with the client

The client serves a sample log over a throwaway HTTP server (standing in for the platform's
signed URL), then calls the tool over MCP.

```bash
cd /home/jaskirat/VISTA-MCP
export MCP_URL="http://127.0.0.1:8100/mcp" MCP_AUTH_TOKEN='vista-test-strong-xyz'

# (a) NEGATIVE routing — an SLA question → DeepSeek runs logv only (skips perf/eos)
.venv/bin/python client_test.py --file test_data/sdwan-small.log \
  --question "SLA failures on the Austin healthcheck at 12:07"

# (b) POSITIVE routing — a performance question → DeepSeek adds the perf analyzer
.venv/bin/python client_test.py --file test_data/sdwan-small.log \
  --question "High CPU and conserve mode — what performance problem is this?"

# (c) EoS routing — → DeepSeek adds the eos analyzer
.venv/bin/python client_test.py --file test_data/sdwan-small.log \
  --question "Is this FortiGate model past its end-of-support date?"

# just list tools (shows the config-driven description)
.venv/bin/python client_test.py --list-only
```

Watch the server terminal (or the GUI) — you'll see the whole flow per call:
`pfr → tool_enablement → discover → decide → analyze:… (parallel) → concat → orb → done`, with
the DeepSeek reason and which analyzers ran.

### Concurrency (multiple clients/files at once)
```bash
export MCP_URL="http://127.0.0.1:8100/mcp" MCP_AUTH_TOKEN='vista-test-strong-xyz'
.venv/bin/python client_test.py --file test_data/sdwan-small.log --question "SLA failures on Austin healthcheck" &
.venv/bin/python client_test.py --file test_data/sdwan-small.log --question "High CPU and conserve mode?" &
.venv/bin/python client_test.py --file test_data/sdwan-small.log --question "End-of-support date for this model?" &
wait
```
All three run concurrently with independent job ids and independent routing (logv / logv+perf /
logv+eos). Open `/gui` to watch them animate side by side.

---

## 4. Poke the GUI/API directly (optional)

```bash
curl -s http://127.0.0.1:8100/gui/api/state | python3 -m json.tool | head        # tools + recent jobs
curl -s http://127.0.0.1:8100/gui/api/jobs/<job_id> | python3 -m json.tool | head # one job's live flow
curl -s http://127.0.0.1:8100/gui/api/config | python3 -m json.tool               # the TOOL_ENABLEMENT config

# save config (validates + hot-reloads). Invalid config is rejected without touching the file:
curl -s -X POST http://127.0.0.1:8100/gui/api/config \
  -H 'Content-Type: application/json' --data @config/tool_enablement.json
```

---

## 5. What "passing" looks like

- **Routing is correct both ways:** SLA/traffic questions → `logv` only; CPU/conserve questions →
  `logv + perf`; EoS questions → `logv + eos`. The `decide` step logs a one-line reason.
- **Skip-DeepSeek logic:** with only mandatory analyzers configured, the `decide` step is
  `skipped` (no DeepSeek call); with ≥1 optional it runs.
- **Parallelism:** selected analyzers show overlapping timings; total ≈ slowest analyzer + ORB.
- **ORB:** appended once as `## ORB Suggestions` when the tool has `orb_enabled` (fail-open — a
  slow/failed ORB never breaks the report).
- **Concurrency:** several jobs run at once with separate ids and correct independent routing.
- **Client sees no behavior change:** the returned report is the same shape as before — the LogV
  analysis + interactive dashboard link + `## ORB Suggestions` (+ any companion sections).

---

## 6. Config knobs (env)

| var | default | meaning |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8100` | bind address |
| `MCP_AUTH_TOKEN` | — (required) | bearer token clients must send; server refuses a weak/default token unless `MCP_ALLOW_INSECURE=1` |
| `MCP_ALLOW_INSECURE` | off | permit a dev/no token (local only) |
| `MCP_ALLOW_PRIVATE_FETCH` | off | allow fetching loopback/LAN log URLs (local testing only) |
| `TOOL_ENABLEMENT_PATH` | `config/tool_enablement.json` | path to the TOOL_ENABLEMENT config |
| `MAX_LOG_BYTES` | `52428800` | streaming fetch cap |
| `MCP_LOG_LEVEL` | `INFO` | `DEBUG` also previews the report text |

*(No pytest suites by design — this is exercised via the CLI flows above and the live GUI.)*
