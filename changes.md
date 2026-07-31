# changes.md — what changed, how, and why

**Revision date:** 2026-07-31
**Scope:** three changes, in this order of importance.

1. **The routing step became a real AI Controller** — it now runs on *every* call and plans each
   analyzer's request from the contract that analyzer just advertised, so an analyzer can change
   its API (or a new one can be added in the GUI) and be called correctly with no code change.
   "DeepSeek" is renamed to **AI Controller** everywhere.
2. **Jobs moved from memory to PostgreSQL** — permanently, with the whole flow: every step, every
   analyzer's discovery + exact request + result, ORB, and the final report.
3. **The GUI was rebuilt** into a three-view operator console (Flow · Dashboard · History) with a
   node inspector, pan/zoom, a resizable verbose CLI log, a guided add-tool flow, an in-app guide,
   and a proper light/dark design system.

> **The end client and the end analyzers see no change.** `client_test.py` is untouched, the MCP
> surface is unchanged (`source_url` + optional `question` → one text report), and the report has
> the same sections in the same order. Verified before and after — see §7.

---

## 1. The AI Controller (was "DeepSeek")

### 1.1 What was wrong

The old `decide` step had one job — pick which *optional* analyzers to add — and a shortcut:

> if there are 0 optional analyzers, the answer is fully determined → **skip the model entirely**.

That shortcut was only sound while a request's *shape* could be assumed. It can't be. Discovery is
live: an analyzer can rename a param, add a required one, or move its endpoint between two calls,
and a brand-new analyzer can be added in the GUI at any moment. Skipping meant calling a
possibly-changed API with a request shape hardcoded in `analyzer_client.py`
(`files["file"]`, `data["question"]`). Discovery was fetched, then largely ignored.

### 1.2 What it does now

The step **always runs** and has two jobs:

| | |
|---|---|
| **Selection** | which optional analyzers add value for this question (from each one's `when_to_use`) |
| **Invocation planning** | for **every** analyzer that will run: method, URL, content type, which param carries the file, which declared params get the question, and what value any other required param should take |

Both are answered from the discovery documents fetched moments earlier. When a tool has no
optional analyzers the controller runs in **`plan-only`** mode — nothing to select, but the live
contracts still get read. Discovery decides *what is possible*; the controller decides *what to do
about it*.

The prompt now carries each analyzer's full live contract:

```
- id="perf2"  (Performance Analyzer v2)  [MANDATORY]
    when_to_use: Use for FortiGate performance questions — CPU/memory pressure, conserve mode…
    call: POST http://127.0.0.1:8813/perf2/v2/analyze   content_type=multipart/form-data
      · param "payload" (required, type=file, location=file) — The file to analyze.
      · param "query" (required, type=string, location=form) — The user's natural-language question.
      · param "mode" (required, type=string, location=form, default='quick') — Analysis depth:
        'quick' for a fast scan, 'deep' for a full correlation pass. Choose 'deep' when the
        question mentions a specific device, interface or metric.
```

and returns, alongside `call` and `reason`, an `invocations` array — one entry per analyzer that
will run. Field values use the placeholders `{{question}}` / `{{filename}}` instead of echoing
content, so a long question can never be truncated or re-quoted on its way to an analyzer.

### 1.3 The model proposes; the discovery document decides

Every proposed plan is validated field-by-field against the same discovery
(`orchestrator/plan.py::from_ai`):

| Field | Rule if it disagrees with discovery |
|---|---|
| `method` | replaced with the discovered method |
| `url` | replaced with the discovered `query.path` (a different host is rejected outright) |
| `content_type` | replaced with the discovered content type |
| `file_param` | replaced with a declared `location:"file"` param |
| `fields` | keys the analyzer never declared are dropped |
| required params | anything missing is filled from discovery (its `default`, else question/filename) |

Every correction is recorded in `CallPlan.notes`, shown on the node in the console, and stored per
analyzer per job — so "what did the model propose vs. what did we send" is always answerable. A
plan ends up marked `ai`, `ai-corrected` or `deterministic`.

**A hallucinated endpoint, verb, host or parameter cannot reach an analyzer.** That is what makes
it safe to let a model plan a request at all.

### 1.4 Fail-safe

If the gateway is disabled (`AI_CONTROLLER_ENABLED=0`), unreachable, or returns junk, the decision
falls back to the deterministic policy: **include every optional analyzer** (better to over-analyze
than to silently drop a relevant one) and build each request straight from its discovery
(`plan.py::deterministic`). For the standard `file` + `question` contract that produces
**byte-for-byte the request VISTA-MCP has always sent** — which is exactly why this change carries
no regression risk.

### 1.5 One additive contract field: `param.default`

While testing the fail-safe path against an analyzer that had added a *new required* param, a real
gap appeared: with no AI and no default, there is nothing correct to send (FastAPI rejects an empty
required form field as missing). So `AnalyzerParam` gained an optional **`default`**:

* the deterministic builder sends it, so the no-AI path stays correct;
* the AI Controller may override it when the question calls for something else.

Additive and v1.0-compatible — analyzers that omit it behave exactly as before.
Documented in `docs/analyzer_api.md` §2 and §7.

### 1.6 The rename

`DeepSeek` → **AI Controller** in code, logs, the GUI, and the docs, because the step is no longer
a single vendor's yes/no on analyzer selection, and the gateway behind it is an implementation
detail.

| Before | After |
|---|---|
| `orchestrator/deepseek.py` | `orchestrator/ai_controller.py` (deleted / replaced) |
| step key `decide`, GUI label "DeepSeek Decision" | step key `ai_controller`, label "AI Controller" |
| `Decision.used_deepseek` / `.deepseek_raw` | `Decision.used_ai` / `.ai_raw` (+ `plans`, `plan_source`, `mode`, `elapsed_ms`) |
| `DEEPSEEK_GEN_URL`, `ANALYSIS_API_URL`, `DEEPSEEK_TIMEOUT` | `AI_CONTROLLER_GEN_URL`, `AI_CONTROLLER_FALLBACK_URL`, `AI_CONTROLLER_TIMEOUT` |

**The legacy env names are still read as a fallback**, so an existing deployment keeps working
without touching its environment.

### 1.7 Cost

One extra gateway call on jobs that used to skip it. Measured on this box the call is 1–20 s
(gateway-dependent, not prompt-dependent — the *old* short prompt measured 33 s cold and 1.2 s
warm), against a 30–120 s analysis + ORB. `AI_CONTROLLER_ENABLED=0` is the escape hatch.
A decision cache keyed by *(tool, discovery fingerprint, normalized question)* is listed as a next
improvement — fingerprinting the discovery keeps it correct, since any contract change busts it.

---

## 2. Durable job history in PostgreSQL

### 2.1 What was wrong

`jobs.py` kept the 30 most recent jobs in an `OrderedDict` and evicted the rest. Restart the
server and the history was gone; there was no way to look back at what an analyzer was sent last
Tuesday, and no analytics of any kind.

### 2.2 What it does now

`orchestrator/db.py` persists everything to Postgres. Four tables (all prefixed `mcp_` — the
`usage_logs` database is shared with other VISTA services):

| Table | One row per | Holds |
|---|---|---|
| `mcp_jobs` | tool call | status, timings, the question/file, mandatory/optional/discovered/dropped/selected ids, AI mode + reason + **full system prompt** + **raw answer** + plan source, ORB status/size/timing, report size and the **final report text**, error, server host |
| `mcp_job_events` | pipeline step event | step, status, detail, timestamp, elapsed, and the step's structured `data` as `jsonb` |
| `mcp_job_analyzers` | analyzer per job | role, discovered/selected/called/ok, HTTP status, `when_to_use`, **the live discovery document**, **the exact request sent** (method, url, content type, file param, fields), plan source + correction notes, timing, and its own report |
| `mcp_config_audit` | TOOL_ENABLEMENT save | who, when, which tools, and the full config |

Analyzers that were discovered but *not* selected are recorded too, so history shows the whole
decision rather than only the winners.

### 2.3 How it stays out of the way

* **Never blocks the pipeline.** Writes go onto an in-process queue drained by **one background
  thread**. A tool call never waits for the database.
* **Never breaks a request.** Every failure is caught, counted and logged (rate-limited). With no
  `DATABASE_URL`, or with the database down, the server behaves exactly as it did before — the
  live console still works, only history/analytics are unavailable (and the console says so).
* **Ordered.** One writer means a job row always lands before its events.
* Each write retries once; the queue is drained on shutdown (best effort).

The in-memory registry stays as the **live window** (`MCP_JOBS_MEMORY`, default 60) so the running
flow is instant; Postgres is the permanent record. A job's detail endpoint serves from memory while
it is live and rebuilds from the database afterwards — including jobs written by a *different*
server instance.

### 2.4 Production

`docs/db_setup.md` has the full instructions and `docs/db_setup.sql` the exact statements to run in
prod `psql` (generated from the same DDL the app uses, so they cannot drift). Summary:

```bash
psql -h <host> -U <admin> -d usage_logs -f docs/db_setup.sql
export DATABASE_URL="postgresql+psycopg://<user>:<pass>@<host>:5432/usage_logs"
export MCP_DB_AUTO_CREATE=0     # schema managed by hand; the app never needs DDL rights
```

New dependency: `psycopg[binary,pool]>=3.1` (added to `pyproject.toml`). The `+psycopg`
SQLAlchemy-style URL suffix is accepted and stripped; passwords are never logged.

---

## 3. The operator console (`/gui`)

Rebuilt from a single flow page into three views. Same typeface stack as before; everything else —
tokens, spacing, contrast, focus states, dark mode — is new.

### 3.1 Flow

* **Pan & zoom** — drag to pan, scroll to zoom at the cursor, double-click to fit, plus
  −/+/fit/100% controls. Fits automatically when the flow changes, never below a readable scale.
* **Node inspector** — click any node. It shows what that step *did*:
  * **AI Controller**: mode (`select+plan` / `plan-only`), whether the AI answered, plan source,
    what was selected and why, **the planned call for every analyzer** (method, URL, file param,
    fields, the model's own note, and any validation corrections), the full routing system prompt
    and the raw AI answer.
  * **Analyzer nodes**: role, HTTP status, timing, **the exact request that was sent**, the
    analyzer's `when_to_use`, its **live discovery document**, result `meta`, and its report.
  * **Discover**: every discovered contract, and anything dropped.
* **CLI log** — drag its top edge to resize (persisted), collapse it, filter by text or status,
  toggle verbose to expand any line into its structured payload, copy the whole log, or open the
  **exact final report** the MCP client received. Each line carries a wall-clock time and a
  relative offset.
* **Deep links** — `#flow`, `#dash`, `#hist`, `#job=<id>`, `#job=<id>&node=<step>`, `#tool=<name>`,
  `#tools`, `#integrations`, `#guide`. A specific node of a specific job can be pasted into a
  ticket.

### 3.2 Dashboard (new)

Filter by range (1h/24h/7d/30d/all), tool and analyzer. KPI tiles (jobs, success rate, median +
p95 + max duration, AI Controller answer rate and how often its plans were used as-is, ORB
coverage, report volume and bytes analyzed), a jobs-over-time chart with errors overlaid and
average duration as a line, per-tool and per-analyzer breakdowns, and recent errors. The
per-analyzer table surfaces **plan corrections** — a rising count is the signal that an analyzer's
contract has drifted. All charts are hand-rolled SVG; no external assets (the page loads offline).

### 3.3 History (new)

The searchable permanent record: free text across question / filename / job id / tool, plus tool,
status, analyzer, AI-usage and time-range filters, with paging. Click any row to replay that job's
flow — nodes, inspector and CLI log — reconstructed from the database, even after a restart.

### 3.4 Add-tool / add-analyzer

* **Guided 3-step wizard** for a new tool (name + description → routing prompt + ORB → analyzers).
* **🔌 Test & autofill** on any analyzer URL: calls `POST /gui/api/probe` → the server fetches
  `GET <base>/discover`, confirms the analyzer speaks the standard contract, shows its
  `when_to_use`, and fills in id and title. Errors are shown verbatim.
* Analyzers are edited as **cards** with a mandatory/optional segmented control instead of a dense
  table row.
* **Client-side validation before save** — tool-name pattern, required id/api_url, duplicate ids,
  and "every tool needs an enabled mandatory analyzer". Server-side validation is unchanged: an
  invalid config is rejected and the file on disk is left untouched.
* **📖 Guide** link next to **Integrations** opens a full in-app walkthrough (also written to
  `docs/gui_guide.md`).

### 3.5 Look and feel

New token system with matched light/dark palettes (dark mode now also follows the OS on first
visit), consistent 8px rhythm, AA-contrast text, hover/focus states on every control, keyboard
support (`Esc` closes, `Alt+1/2/3` switch views, nodes and job rows are focusable), toasts instead
of `alert()`, and empty states that explain what to do next.

---

## 4. Files

**Added**

| Path | Purpose |
|---|---|
| `orchestrator/ai_controller.py` | the AI gateway client (ask + JSON extraction), fail-safe; replaces `deepseek.py` |
| `orchestrator/plan.py` | build a `CallPlan` from discovery · validate an AI plan against it |
| `orchestrator/db.py` | durable history in Postgres (queued, one writer, fail-open) + the read queries |
| `fakes/fake_analyzer_v2.py` | the standard contract with a **different request shape** — the dynamic-discovery test |
| `docs/db_setup.md` · `docs/db_setup.sql` | production database setup, verification, retention, troubleshooting |
| `docs/gui_guide.md` | the console walkthrough (mirrors the in-app guide) |
| `changes.md` | this file |

**Removed:** `orchestrator/deepseek.py`.

**Changed:** `decide.py` (rewritten), `analyzer_client.py` (executes a validated plan; returns the
HTTP status), `pipeline.py` (always-on AI Controller, per-analyzer records, richer telemetry),
`jobs.py` (write-through to the DB, `set_meta`, `add_analyzer_run`), `models.py` (`CallPlan`,
`AnalyzerRun`, richer `Decision`/`Job`, `AnalyzerParam.default`), `discovery.py` (`probe()`),
`gui.py` (history / analytics / facets / probe routes), `gui/index.html` (rebuilt), `server.py`
(DB init + shutdown, banner), `tool_enablement.py` and `orb.py` (comments), `fakes/run_fakes.sh`
(starts `perf2`), `pyproject.toml`, README and all docs.

---

## 5. New environment variables (production)

Add these to the prod `.env` — both blocks are already appended, with comments, to `.env.prod`.

```bash
# --- AI Controller (was "DeepSeek") ---------------------------------------
AI_CONTROLLER_ENABLED=1                 # 0 = deterministic routing + discovery-built requests
AI_CONTROLLER_GEN_URL=https://vista.fortinet.com/ai-mcp/ds4/generate
AI_CONTROLLER_FALLBACK_URL=https://vista.fortinet.com/ai-mcp/generate
AI_CONTROLLER_TIMEOUT=60
# AI_TOOL=IKE
# legacy DEEPSEEK_GEN_URL / ANALYSIS_API_URL / DEEPSEEK_TIMEOUT are still honoured

# --- Durable job history (Postgres) ---------------------------------------
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@DB_HOST:5432/usage_logs
MCP_DB_ENABLED=1
MCP_DB_AUTO_CREATE=0                    # create the schema with docs/db_setup.sql first
MCP_DB_POOL_MAX=6
MCP_DB_STORE_REPORTS=1                  # 0 = store report lengths but not the text
MCP_DB_QUEUE_MAX=20000                  # write queue depth; writes never block a tool call
MCP_JOBS_MEMORY=60                      # live in-memory job window for the console
```

Nothing else changes. Every one of these has a safe default: with none of them set the server runs
exactly as it did before, minus history (and with the AI Controller pointed at the same gateway as
the old `DEEPSEEK_GEN_URL` default).

**Deployment order:** create the schema → set the variables → restart. The startup banner confirms
both subsystems:

```
  AI Controller    : ON → https://vista.fortinet.com/ai-mcp/ds4/generate  (timeout 60s)
  job history (DB) : ON → postgresql://user:***@host:5432/usage_logs
```

---

## 6. Compatibility

| Surface | Status |
|---|---|
| MCP tool surface (`source_url` + `question` → one text report) | **unchanged** |
| Report content and section order | **unchanged** |
| `client_test.py` | **untouched** — same file, same flags, same command line |
| Analyzer contract (`/discover` + `POST <query.path>` → `AnalyzerResult`) | **unchanged**; `param.default` is additive and optional |
| LogV, perf, eos analyzers | **untouched** — they receive the same requests as before |
| `config/tool_enablement.json` | **unchanged** schema |
| Legacy `DEEPSEEK_*` env vars | still honoured |
| Auth, SSRF guard, TLS handling, streaming/size cap | **unchanged** |

---

## 7. Testing — before and after

Full commands: `docs/testing.md`. Environment: real LogV backend on :8802 (genuine AI analysis),
fake `perf` :8811, `eos` :8812, `perf2` :8813, MCP server :8100, Postgres `usage_logs`.

### 7.1 Regression (the client-visible contract)

A **baseline was captured on the pre-change code first**, then repeated after:

```bash
python client_test.py --file test_data/DEMO_LOG_VISUALIZER_SDWAN.log \
  --question "SLA failures for LO_C1_2 and High CPU "
```

| Check | Before | After |
|---|---|---|
| Report section headings + order | `# Log Analyzer & Visualizer` → `## Filter` → `## View in Log Visualizer` → `## Analysis` → `## Interactive Visualization` → `# Performance Analyzer` → `## ORB Suggestions` | **identical** (diffed heading-by-heading) |
| Routing for that question | `logv + perf` | `logv + perf` |
| Request sent to logv | `POST …/agent_assist/run`, file→`file`, `question` | **identical** |
| Request sent to perf | `POST …/perf/run`, file→`file`, `question` | **identical** |
| 13.9 MB log, 29,169 logs, filter 263 matched | ✔ | ✔ |
| ORB appended | ✔ | ✔ |

Also re-verified unchanged: `--list-only`, `--no-question` (no Filter block, as before), an
unsupported log type (`Traffic:forward` → "not supported yet" + ORB), and the
all-analyzers-unreachable path (friendly message, job recorded as `error` with the dropped ids).

**Concurrency:** three simultaneous clients → three independent job ids and three independent
routing decisions (SLA → `logv`; High CPU → `logv + perf`; end-of-support → `logv`, correctly
adding nothing because no EoS analyzer is configured on that tool).

### 7.2 The new dynamic behaviour

Driven with `fakes/fake_analyzer_v2.py`, whose contract differs on purpose: file param `payload`,
question param `query` (required), extra required `mode` (default `quick`), endpoint
`/perf2/v2/analyze`.

| Check | Result |
|---|---|
| Mandatory-only tool | `ai_controller` step is **ok**, `mode=plan-only` — no longer skipped |
| Changed contract | `POST …/perf2/v2/analyze  (file → payload, fields=['mode','query'], plan=ai)` — the analyzer echoed back exactly what it received |
| Value chosen from a param description | question naming an interface → `mode='deep'`; vague question → `quick` |
| Two contracts in one job | `perf` called with `file`+`question` **and** `perf2` with `payload`+`query`+`mode`, in the same run |
| Selection still precise | CPU question → adds `perf2`; end-of-support question → adds `eos`; never both |
| Validation | a plan with a foreign host, wrong verb, undeclared field and wrong file param was corrected to the discovered values, marked `ai-corrected`, and every change recorded |
| Fail-safe (gateway dead) | `AI Controller unavailable → fail-safe`, all optional analyzers ran, requests built from discovery (`plan=deterministic`), `mode=quick` from the advertised default, client output normal |
| `AI_CONTROLLER_ENABLED=0` | same deterministic behaviour without contacting the gateway |

### 7.3 Persistence

10 jobs across two server instances: **167 events, 21 analyzer rows, 0 failed writes, 0 dropped,
0 jobs stuck in `running`, 0 empty reports.** A job produced by one server instance was opened in
another instance's console and replayed in full — nodes, per-analyzer requests, events, AI prompt
and raw answer, final report. `docs/db_setup.sql` was run twice against the live database to
confirm it is idempotent.

### 7.4 Console

All endpoints exercised (`state`, `jobs`, `jobs/{id}` from both memory and DB, `analytics`,
`facets`, `config` GET/POST, `probe` for both a good and a dead URL), and all three views plus the
inspector, config editor and guide were rendered and visually checked in light and dark mode at
1680×1000. `/gui` and `/mcp/gui` both serve.

---

## 8. Known limitations / next

* **The AI Controller adds one gateway call to every job** (§1.7). Mitigations: it is fail-safe,
  it can be disabled, and a discovery-fingerprinted decision cache is the planned optimisation.
* **`/gui` is still unauthenticated** — it is an operator console; bind it to a trusted network or
  front it with your own auth. Unchanged from before, but the console now exposes history, so the
  stakes are higher.
* **VFR is still a passthrough.** Untouched by this revision; it remains the highest-priority
  functional gap (`docs/Architecture.md` §11).
* **JSON-body analyzers** are supported by the executor but untested end to end — every analyzer
  today is multipart.
* **No retention job.** History grows unbounded by design; `docs/db_setup.md` §7 gives the queries
  if a policy is ever wanted.

---

# Revision 2 — 2026-07-31 (console follow-up)

Four items, all in the operator console.

## R2.1 Inspector / log panels closed themselves after ~1.5 s (bug)

**Symptom:** expanding *Live discovery document*, *This analyzer's report*, *Raw AI answer* — or
any expanded CLI-log line — showed the content for about a second, then collapsed.

**Cause:** the 1.5 s poll re-fetched the selected job unconditionally and rewrote
`#inspBody`/`#clog` via `innerHTML`. Rebuilding the DOM discards `<details open>`, scroll position
and expanded payloads. Nothing was wrong with the click handler — the panel underneath it was
being replaced.

**Fix, three layers:**

1. **A finished job is never re-fetched.** `poll()` now only reloads the selected job when it is
   missing, different, or still `running`. Measured: 12 poll cycles on a completed job produce
   **1** `GET /gui/api/jobs/<id>` instead of 12.
2. **Repaint only on change.** Both panels build their HTML first and compare it with what is
   already rendered; identical output means the DOM is left completely untouched. This covers
   running jobs, where re-fetching is genuinely necessary.
3. **State is carried across a real repaint.** When content *has* changed, which folds were open,
   where the panel was scrolled, and which log lines were expanded are all restored afterwards.

**Plus the requested pop-out:** every expandable block now has a **⤢** button that opens its
content in a window that stays put — with a copy button — independent of anything the page does
underneath. Block ids are derived from the node key and title, so they stay stable across
re-renders.

## R2.2 Recent-jobs sidebar now reads from the database

`GET /gui/api/state` returns **live in-memory jobs first, then the most recent jobs from
Postgres**, de-duplicated and sorted newest-first, plus a `jobs_total` count. The sidebar
therefore survives a restart and shows jobs written by another instance, instead of only what this
process happens to hold in memory. Each row gained a relative timestamp ("31m ago").

Scrolling to the end of the list lands on a footer that states how many of how many jobs are shown
and offers **🗂 Open full History →**.

## R2.3 Two standalone documentation pages, in their own tabs

The in-app guide modal is gone. The server now serves two real pages, under **both** prefixes:

| Page | URL | Source |
|---|---|---|
| **User Guide** | `/gui/user-guide` · `/mcp/gui/user-guide` | `gui/user_guide.html` |
| **API Integration Guide** | `/gui/api-integration-guide` · `/mcp/gui/api-integration-guide` | `gui/api_guide.html` |

The header links (**📖 User Guide ↗**, **🔌 API Integration ↗**, next to Integrations) open them in
a new tab; their hrefs are derived from the serving prefix, so they work at `/gui` and `/mcp/gui`
alike. Both pages are self-contained (sticky table of contents, numbered sections, copy buttons,
light/dark following the OS) and share the console's design tokens and typeface.

The **API Integration Guide** is the piece that was missing: it documents the exact API a service
must expose to be usable as an analyzer — the two endpoints, every field of the discovery document
and why it matters, the result contract, copy-paste Pydantic models, a complete working analyzer,
how the AI Controller consumes it, the rules for changing the API later, how to register it in the
console, and a pre-flight checklist with `curl` commands.

## R2.4 Dashboard: filtering and drill-down

* **Filters added:** free-text search (question / filename / job id / tool), status, AI usage and
  ORB outcome, alongside the existing range / tool / analyzer. All were already supported by the
  analytics endpoint; only the controls were missing.
* **Filter summary line** under the bar states the slice in words ("Showing 15 jobs · last 24
  hours"), renders each active filter as a removable chip, and offers **Clear all** / **Reset**.
* **KPIs split and made clickable** — *Succeeded* and *Errors* are now separate tiles; clicking
  *Jobs*, *Succeeded*, *Errors* or *AI Controller* opens **History** with the same filters applied.
* **Tables drill down** — clicking a row in *By tool* or *By analyzer* narrows the whole dashboard
  to it.
* **Timeline reads as time.** Empty buckets are drawn (the API only returns buckets that had
  jobs), so gaps are visible, and the average-duration line breaks across quiet periods instead of
  diving to zero.

## R2.5 Files

**Added:** `gui/user_guide.html`, `gui/api_guide.html`.
**Changed:** `gui/index.html` (poll gating, repaint guards, fold state, pop-out blocks, sidebar,
dashboard), `orchestrator/gui.py` (`/user-guide` + `/api-integration-guide` routes, DB-merged
`/api/state`), `docs/gui_guide.md`, `README.md`.
No API, analyzer, database-schema or environment changes — nothing in §5 or §6 of revision 1
moves.
