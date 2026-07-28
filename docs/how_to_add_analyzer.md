# How to add an analyzer (and how to add a whole new tool)

The whole point of the rearchitecture: **adding capability is a config edit, not code.** There
are two things you might add.

- **Add an analyzer to an existing tool** → edit config only. (§1)
- **Add a brand-new MCP tool** → one tiny tool function in `server.py` + config. (§2)

Prerequisite for either: the analyzer must speak the **standard contract** in
[`analyzer_api.md`](analyzer_api.md) — `GET <base>/discover` and `POST <query.path>`. If it does,
the orchestrator can already discover it, let DeepSeek pick it, call it, and fold in its report.
Nothing in `orchestrator/` changes.

---

## 1. Add an analyzer to an existing tool

### 1a. Make sure your analyzer follows the contract
It must expose the two endpoints and return `AnalyzerResult` with a fully-formatted
`report_markdown`. Copy `fakes/fake_analyzer.py` as a starting point — it's a complete, correct
analyzer in ~60 lines. The single most important field is **`analyzer.when_to_use`** in your
discovery: it is exactly what DeepSeek reads to decide whether to call you. Write it like a
routing rule ("Use for … / do NOT use for …").

### 1b. Add one entry to `config/tool_enablement.json`
Under the tool's `analyzers` array:

```jsonc
{
  "id": "perf",                       // stable short id (also the analyzer's own id)
  "title": "Performance Analyzer",
  "api_url": "https://vista.fortinet.com/perf",   // your analyzer's base URL
  "discover_url": "",                 // empty ⇒ derived as api_url + "/discover"
  "mandatory": false,                 // false ⇒ DeepSeek decides; true ⇒ always called
  "enabled": true,                    // false ⇒ ignored entirely
  "timeout": 120
}
```

- **`mandatory: true`** — always called for this tool (e.g. LogV's own analysis).
- **`mandatory: false`** — called only when this tool's DeepSeek routing selects it, based on
  your `when_to_use`.
- If, after this edit, the tool has **≥1 optional** analyzer, DeepSeek routing turns on
  automatically; with **0 optional** it stays skipped. You don't manage that — it's derived.

### 1c. (Optional) tune the tool's routing so DeepSeek picks it well
Edit that tool's **`routing_system_prompt`** to mention when the new companion is relevant. This
is the per-tool DeepSeek guidance; keep it about *routing* (the fixed JSON output format is added
automatically). Example addition: *"…for questions about interface throughput or bandwidth
saturation, add the performance analyzer."*

### 1d. Apply it
- **Via the GUI (easiest):** open `http://<host>:<port>/gui` → **Add / Edit Tools** → under the
  tool, **+ add analyzer**, fill the row, **Save & reload**. It validates and hot-reloads — no
  restart.
- **By hand:** edit the JSON and either restart the server or `POST /gui/api/config` (the GUI
  Save does this). The loader also hot-reloads via `tool_enablement.reload()`.

That's it. Discovery, DeepSeek selection, the parallel call, concatenation, and ORB all work with
your analyzer immediately.

---

## 2. Add a brand-new MCP tool — **GUI only, no code, no restart**

A "tool" is what the MCP client sees in `list_tools`. Tools are now **fully config-driven**:
every VISTA tool shares the same MCP surface (`source_url` + optional `question`), so `server.py`
registers one generic function per configured tool and re-syncs the live tool set whenever the
config changes. **Adding a tool is a config edit — the new MCP tool (name + endpoints) appears
immediately, with no code and no restart.**

### The one supported way: the GUI
Open `/gui` (or `/mcp/gui`) → **Add / Edit Tools** → **+ add tool**:
1. Give it a name (this is the `list_tools` name, e.g. `Config_Generator`).
2. Fill its **description** (what clients read), its **routing system prompt** (its DeepSeek
   analyzer-selection prompt), and toggle **ORB**.
3. Add its **analyzers** (id, title, api_url, mandatory/optional, enabled, timeout).
4. **Save & reload.**

On save the server validates the config, writes `config/tool_enablement.json`, and calls
`sync_tools()` (via a config-change listener) which registers the new MCP tool live. The tool is
now in `list_tools`, callable, and has its own flow in the GUI. Removing a tool unregisters it;
editing its description refreshes it — all without a restart.

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
- [ ] `when_to_use` is written as a precise routing rule
- [ ] Tool/analyzer added **in the GUI** (Add / Edit Tools) — description, routing prompt, analyzers
- [ ] (optional) tool's `routing_system_prompt` mentions the new companion
- [ ] **Save & reload** — verified live in `/gui`, the Integrations popup, and `list_tools` (no restart)

*(The older `docs/ADDING_ANALYZERS.md` describes the pre-rearchitecture subclass pattern and is
superseded by this document and `analyzer_api.md`.)*
