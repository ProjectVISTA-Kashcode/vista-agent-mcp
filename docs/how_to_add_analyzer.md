# How to add an analyzer (and how to add a whole new tool)

The whole point of the rearchitecture: **adding capability is a config edit, not code.** There
are two things you might add.

- **Add an analyzer to an existing tool** → edit config only. (§1)
- **Add a brand-new MCP tool** → one tiny tool function in `server.py` + config. (§2)

Prerequisite for either: the analyzer must speak the **standard contract** in
[`analyzer_api.md`](analyzer_api.md) — `GET <base>/discover` and `POST <query.path>`. If it does,
the orchestrator can already discover it, let the **AI Controller** pick it *and work out how to
call it*, then fold in its report. Nothing in `orchestrator/` changes.

> Your analyzer does **not** have to use the `file` + `question` shape. Declare whatever params
> you accept in your discovery and they will be used — see
> [`analyzer_api.md` §7](analyzer_api.md#7-changing-your-api-without-breaking-callers).

---

## 1. Add an analyzer to an existing tool

### 1a. Make sure your analyzer follows the contract
It must expose the two endpoints and return `AnalyzerResult` with a fully-formatted
`report_markdown`. Copy `fakes/fake_analyzer.py` as a starting point — it's a complete, correct
analyzer in ~60 lines. (`fakes/fake_analyzer_v2.py` shows the same contract with a completely
different request shape.) Two fields carry the most weight:

- **`analyzer.when_to_use`** — exactly what the AI Controller reads to decide whether to call you.
  Write it like a routing rule ("Use for … / do NOT use for …").
- **`query.params[].description`** (and `default`) — what the AI Controller reads when it has to
  choose a value for a param. Any **required** param should also declare a `default`, so the
  no-AI fallback path still sends something correct.

### 1b. Add one entry to `config/tool_enablement.json`
Under the tool's `analyzers` array:

```jsonc
{
  "id": "perf",                       // stable short id (also the analyzer's own id)
  "title": "Performance Analyzer",
  "api_url": "https://vista.fortinet.com/perf",   // your analyzer's base URL
  "discover_url": "",                 // empty ⇒ derived as api_url + "/discover"
  "catalog_select": "",               // only for a CATALOG /discover — see below
  "mandatory": false,                 // false ⇒ the AI Controller decides; true ⇒ always called
  "enabled": true,                    // false ⇒ ignored entirely
  "timeout": 120
}
```

**If the URL advertises several analyzers (a catalog).** Some services publish their whole surface
from one `/discover` and have no per-route discovery — ORB is the example
(`https://vista.fortinet.com/orb/discover` offers `config-extract` and `config-validate`). Point
`api_url` at the **catalog** and use `catalog_select` to say which route this entry means:

```jsonc
{ "id": "config-validate", "api_url": "https://vista.fortinet.com/orb",
  "catalog_select": "config-validate", "mandatory": true, "enabled": true, "timeout": 180 }
```

Leave `catalog_select` empty to expand **every** route in the catalog into its own runtime
analyzer (`<entry id>:<route id>`), all inheriting this entry's mandatory/enabled/timeout. Press
**🔌 Test & autofill** on a catalog URL and the console lists the routes it offers and fills
`catalog_select` in for you.

- **`mandatory: true`** — always called for this tool (e.g. LogV's own analysis).
- **`mandatory: false`** — called only when this tool's AI Controller routing selects it, based
  on your `when_to_use`.
- The AI Controller runs either way — with 0 optional analyzers it still reads your live contract
  and plans the call (`plan-only` mode). You don't manage that; it's automatic.

### 1c. (Optional) tune the tool's routing so the AI Controller picks it well
Edit that tool's **`routing_system_prompt`** to mention when the new companion is relevant. This
is the per-tool guidance; keep it about *routing* (the fixed JSON output format is added
automatically). Example addition: *"…for questions about interface throughput or bandwidth
saturation, add the performance analyzer."*

### 1c-bis. If your tool takes text instead of a file

Every VISTA tool has historically required the platform-injected `source_url`. A tool whose
analyzers work on *text* — validating a config snippet the user pasted into their question, say —
sets `"require_source_url": false` on the **tool** (not the analyzer). Its MCP input schema then
has `source_url` as an optional field, and the tool can be called with `question` alone. In the
console this is the **"requires an uploaded file"** switch on the tool.

When a file *is* present, a JSON-contract analyzer still gets it: declare a body param for the
content and the AI Controller plans `{{file_text}}` into it.

### 1d. Apply it
- **Via the GUI (easiest):** open `http://<host>:<port>/gui` → **Add / Edit Tools** → under the
  tool, **＋ add analyzer**, paste the base URL, press **🔌 Test & autofill** (it calls your
  `/discover`, confirms the contract and fills in id/title while showing your `when_to_use`), pick
  mandatory/optional, **Save & reload**. It validates and hot-reloads — no restart.
- **By hand:** edit the JSON and either restart the server or `POST /gui/api/config` (the GUI
  Save does this). The loader also hot-reloads via `tool_enablement.reload()`.

That's it. Discovery, selection, invocation planning, the parallel call, concatenation, and ORB
all work with your analyzer immediately. See [`gui_guide.md`](gui_guide.md) for the console.

---

## 2. Add a brand-new MCP tool — **GUI only, no code, no restart**

A "tool" is what the MCP client sees in `list_tools`. Tools are now **fully config-driven**:
every VISTA tool shares the same MCP surface (`source_url` + optional `question`), so `server.py`
registers one generic function per configured tool and re-syncs the live tool set whenever the
config changes. **Adding a tool is a config edit — the new MCP tool (name + endpoints) appears
immediately, with no code and no restart.**

### The fastest way: ✨ Build with AI
Open `/gui` → **Add / Edit Tools** → **✨ Build with AI**, paste the analyzer's base URL, and add
an optional hint ("expose only the validation route", "this one should be mandatory"). The server
fetches the `/discover` — single analyzer or catalog — and the **pro** model drafts the whole
entry: tool name, client-facing description, routing prompt, ORB toggle, `require_source_url`, and
one analyzer entry per route. The draft is validated against the discovery it came from (a
`catalog_select` that names no real route is dropped; a `discover_url` that isn't a discovery
endpoint is fixed) and any corrections are shown.

**It is a draft.** It lands in the editor for you to review and change, and reaches `list_tools`
only when you press **Save & reload**. Treat the generated *description* as the thing most worth
editing — it is the entire basis a calling agent has for choosing your tool.

### The manual way: the GUI wizard
Open `/gui` (or `/mcp/gui`) → **Add / Edit Tools** → **+ add tool**:
1. Give it a name (this is the `list_tools` name, e.g. `Config_Generator`).
2. Fill its **description** (what clients read), its **routing system prompt** (its AI Controller
   analyzer-selection prompt), and toggle **ORB**.
3. Add its **analyzers** — paste each base URL and press **🔌 Test & autofill**, then mark
   mandatory/optional and set a timeout.
4. **Create tool.**

On save the server validates the config, writes `config/tool_enablement.json`, calls
`sync_tools()` (via a config-change listener) which registers the new MCP tool live, and records
an audit row in the database. The tool is now in `list_tools`, callable, and has its own flow in
the console. Removing a tool unregisters it; editing its description refreshes it — all without a
restart.

> This is the intended workflow: **the GUI is the only place tools, analyzers, and prompts are
> added or changed.** Hand-editing `config/tool_enablement.json` still works (the GUI just edits
> that file), but the GUI validates and hot-reloads for you.

### What makes this possible (no per-tool code)
Every tool uses the same generic entrypoint in `server.py`: fetch the injected `source_url`
(shared SSRF/TLS/size-capped fetch) → `pipeline.run(tool_name=<the tool>, …)`. The tool's
identity is just its name; the pipeline loads that tool's analyzers/routing/ORB from config. So
one function serves any tool, and `sync_tools()` reconciles the registered MCP tools with the
config at startup and on every save.

> **Edge case:** if a *future* tool ever needs different typed inputs than `source_url` +
> `question`, that one tool would need a bespoke function. Every VISTA analysis tool to date fits
> the shared shape, so this hasn't been needed.

---

## 3. Checklist

- [ ] Analyzer implements `GET /discover` + `POST <query.path>` per [`analyzer_api.md`](analyzer_api.md)
- [ ] if its `/discover` is a **catalog**, each config entry sets `catalog_select`
- [ ] a text-input tool sets `"require_source_url": false`
- [ ] `when_to_use` is written as a precise routing rule
- [ ] every param is declared, with a `description`; every **required** param has a `default`
- [ ] **🔌 Test & autofill** in the GUI returns green for the analyzer's base URL
- [ ] Tool/analyzer added **in the GUI** (Add / Edit Tools) — description, routing prompt, analyzers
- [ ] (optional) tool's `routing_system_prompt` mentions the new companion
- [ ] **Save & reload** — verified live in `/gui`, the Integrations panel, and `list_tools` (no restart)
- [ ] the job's AI Controller node shows the request that was planned for your analyzer

*(The older `docs/ADDING_ANALYZERS.md` describes the pre-rearchitecture subclass pattern and is
superseded by this document and `analyzer_api.md`.)*
