# The VISTA-MCP console (`/gui`)

The operator surface for the orchestrator. Watch every job execute step by step, see exactly what
each analyzer was sent and returned, search the whole history, and add or change tools, analyzers
and routing prompts **without touching code or restarting the server**.

> The same content is served as a **standalone page** by the server itself — the
> **📖 User Guide ↗** link next to **Integrations** opens it in a new tab. A second page,
> **🔌 API Integration ↗**, documents the API an analyzer must expose to plug in.

| Page | Served at | Repo copy |
|---|---|---|
| Console | `/gui` · `/mcp/gui` | `gui/index.html` |
| User Guide | `/gui/user-guide` · `/mcp/gui/user-guide` | `gui/user_guide.html` (this doc) |
| API Integration Guide | `/gui/api-integration-guide` · `/mcp/gui/api-integration-guide` | `gui/api_guide.html` ([`analyzer_api.md`](analyzer_api.md)) |

Everything is served under **both** prefixes — `/gui` directly, and `/mcp/gui` behind the
production proxy that only forwards `/mcp/*`. Each page derives its links and API base from its
own URL, so both work identically.

---

## 1. Layout

Three views, switched in the header (or with `Alt+1/2/3`):

| View | What it's for |
|---|---|
| **⛓ Flow** | The live n8n-style canvas — one job or one tool's template, animating as it runs |
| **📈 Dashboard** | Aggregate analytics over the stored history: volume, success rate, latency, per-tool and per-analyzer behaviour, recent errors |
| **🗂 History** | The searchable permanent record — filter, find a job from any point in the past, click to replay its flow |

Everything is deep-linkable, so a view or a specific job can be pasted into a ticket:

```
/gui#flow          /gui#dash          /gui#hist
/gui#job=<job_id>                     a specific job's flow
/gui#job=<job_id>&node=ai_controller  …with a node's inspector already open
/gui#tool=<Tool_Name>                 a tool's template flow
/gui#tools   /gui#integrations
```

---

## 2. Flow view

* **Tool tabs** (top) preview a tool's flow *shape* before any job runs — its analyzers, whether
  ORB is on, whether it has a custom routing prompt.
* **Recent jobs** (left) lists in-flight and past runs, newest first, **read from the database** —
  so it survives a restart and includes jobs produced by another instance. Click one to watch it
  animate (or replay it). Scroll to the bottom of the list for a button into the full **History**.
* **Canvas**
  * **drag** to pan, **scroll** to zoom, **double-click** empty space to fit,
    or use the zoom controls (bottom right: −, +, fit, 100%).
  * **click any node** to open the inspector.
* **Inspector** (right) — what that step actually did:
  * **AI Controller** — mode (`select+plan` / `plan-only`), whether the AI answered, the plan
    source, what it selected and why, the **planned call for every analyzer** (method, URL,
    which param carried the file, which fields were sent), plus the full routing system prompt
    and the raw AI answer.
  * **Analyzer nodes** — role, HTTP status, timing, **the exact request that was sent**, any
    corrections validation applied, the analyzer's `when_to_use`, its **live discovery document**,
    its result `meta`, and its own report section.
  * **Discover** — every discovered contract, and anything that was dropped.

  Every expandable block has a **⤢** button that opens it in its own window — useful for long
  discovery documents and reports, and it stays open while a job keeps streaming events. The
  inspector only re-renders when its content actually changes, so open blocks, scroll position and
  expanded log lines are never lost under it.
* **CLI log** (bottom) mirrors the server terminal for the selected job:
  * drag its top edge to **resize**, or collapse it with **▾**
  * **filter** by text, or by status (running / ok / skipped / error)
  * **verbose** adds an expander on every line — click `▸ N fields` to see that event's
    structured payload
  * **📄 report** opens the exact text the MCP client received; **copy** copies the whole log

---

## 3. Add a new MCP tool

### 3a. ✨ Build with AI (the fast path)

**Add / Edit Tools → ✨ Build with AI.** Paste an analyzer's base URL, optionally add a hint, and
press **Draft it**. The server fetches its `/discover` — a single analyzer **or a catalog of
several** — and the `pro` model drafts the whole entry: name, client-facing description, routing
prompt, ORB toggle, whether it needs an uploaded file, and one analyzer entry per route.

The draft is checked against the discovery it came from before you see it (a `catalog_select` that
names no real route is dropped, a `discover_url` that isn't a discovery endpoint is fixed), and any
corrections are listed in the dialog.

**Nothing is saved.** The draft is merged into the editor behind the dialog so you can review and
change every field — the *description* especially, since it is the entire basis a calling agent
has for choosing the tool. It reaches `list_tools` only when you press **Save & reload**. If a
tool of that name already exists you are asked whether to add a copy or replace it.

### 3b. The wizard (the manual path)

**Add / Edit Tools → ＋ New tool** (or the **＋ add tool** chip in the tool bar) opens a 3-step
wizard:

1. **Name + description.** The name is the exact `list_tools` name agents call
   (letters, digits, `_`, `-`). The description is what the calling agent reads to decide whether
   to call it — say what it does and that the platform injects the file.
2. **Routing prompt + ORB.** The routing prompt is this tool's own guidance for the AI Controller
   when it picks optional analyzers; the fixed JSON output contract is appended automatically, so
   you can write freely. Toggle whether ORB troubleshooting is appended to this tool's report.
3. **Analyzers.** Paste a base URL and press **🔌 Test & autofill** — the console fetches
   `GET <base>/discover`, confirms the analyzer speaks the standard contract, shows its
   `when_to_use`, and fills in its id and title. Mark each **mandatory** or **optional**.
   If the URL is a **catalog**, the panel lists every route it advertises and fills in
   `catalog_select`; add another analyzer entry with the same `api_url` and a different
   `catalog_select` to use more of them, or clear the field to expand them all.

Two switches worth knowing on the tool card:

- **requires an uploaded file** — on (default) means the MCP tool's `source_url` input is
  required, as it has always been. Off makes it optional, for a tool whose analyzers take text —
  e.g. validating a config snippet pasted into the question.
- **ORB** — whether ORB troubleshooting is appended to this tool's report. Usually on for
  log/diagnostic tools, off for config authoring or validation.

On **Create tool** the server validates the config, writes `config/tool_enablement.json`,
re-registers the live MCP tool set and audits the change. The tool is callable immediately — it
appears in `list_tools`, gets its own flow, and shows up under **Integrations**. No restart.

---

## 4. Add an analyzer to an existing tool

**Add / Edit Tools** → find the tool → **＋ add analyzer** → paste the base URL →
**🔌 Test & autofill**.

The probe result shows the analyzer's **`when_to_use`** — the single most important field, because
that is exactly what the AI Controller reads when deciding whether to call it. Then choose:

* **mandatory** — always called for this tool.
* **optional** — called only when the AI Controller judges it relevant to the question.

Nothing else is needed: discovery, selection, the parallel call, concatenation and ORB all work
with your analyzer immediately, as long as it speaks the contract in
[`analyzer_api.md`](analyzer_api.md).

**Validation before save** (the file on disk is never touched if it fails):

* tool names match `[A-Za-z0-9_-]{1,64}`
* every analyzer has an id and an `http(s)` `api_url`
* no duplicate analyzer ids inside a tool
* every tool has at least one **enabled mandatory** analyzer — otherwise a call could run nothing

**⟷ Raw JSON** switches the editor to the underlying `tool_enablement.json` and back, for bulk
edits or copy-paste between environments.

---

## 5. Dashboard

Filter by **range** (1h / 24h / 7d / 30d / all), **free text** (question, filename, job id, tool),
**tool**, **analyzer**, **status**, **AI usage** and **ORB outcome**. A summary line under the
filter bar always states, in words, which slice is on screen; each active filter is a chip you can
remove there, and **Reset** clears everything.

* **KPI row** — jobs, succeeded, errors, median duration (with p95 and max), how often the AI
  Controller answered and how often its plans were used as-is, ORB coverage, total report volume
  and bytes of logs analyzed. **Jobs / Succeeded / Errors / AI Controller are clickable** — they
  open History with the same filters already applied.
* **Jobs over time** — job count per bucket with errors overlaid and average duration as a line.
  Empty buckets are drawn, so the x-axis reads as continuous time and the duration line breaks
  across quiet periods rather than diving to zero.
* **By tool** — volume, errors and average duration per MCP tool. **Click a row** to narrow the
  whole dashboard to that tool.
* **By analyzer** — how often each analyzer was selected, its OK/failed counts, average call
  time, and how many of its requests were planned by the AI Controller (with corrections in
  brackets). A rising correction count is the signal that an analyzer's contract has drifted.
  **Click a row** to narrow the dashboard to that analyzer.
* **Recent errors** — click any row to open that job's flow.

---

## 6. History

The permanent record. Filter by free text (question, filename, job id, tool), tool, status,
analyzer, time range, and whether the AI Controller answered. Click any row to replay that job's
flow — nodes, inspector and CLI log exactly as they were, reconstructed from the database even if
the server has restarted since.

---

## 7. Troubleshooting

| What you see | What it means |
|---|---|
| **"dropped" on the Discover node** | That analyzer's `/discover` didn't answer; it was skipped for this run. Use **Test & autofill** in the editor to see the exact error. |
| **AI badge says `fail-safe`** | The AI gateway didn't answer. Every optional analyzer ran and each request was built from discovery — the job completed normally. |
| **`plan corrected` notes on a node** | The model proposed something the analyzer never advertised; the orchestrator replaced it with the discovered value. Each change is listed. |
| **Dashboard/History say "not connected"** | `DATABASE_URL` isn't set or the DB is unreachable — see [`db_setup.md`](db_setup.md). Live flow still works. |
| **Save rejected** | The config failed validation; the file on disk was left untouched and the message names the problem. |
| **`live` indicator says "reconnecting…"** | The page can't reach the server. It retries every 1.5 s. |

---

## 8. Security

The `/gui` routes are **not** behind the MCP bearer token — this is a local operator console.
Bind the server to localhost or a trusted network, or front it with your own auth. Two routes are
not read-only:

* `POST /gui/api/config` — validates before writing, and records an audit row.
* `POST /gui/api/probe` — performs a server-side `GET` on the URL you type (http/https only), so
  the editor can confirm an analyzer's contract.
