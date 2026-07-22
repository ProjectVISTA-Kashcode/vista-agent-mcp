# Adding a new VISTA tool to the MCP

VISTA-MCP exposes a growing catalog of varied tools (log analysis today; script generators,
debug-command helpers, End-of-Support lookups, … later). There are two shapes:

- **Backend-backed / log-forwarding tools** (like `Log_Analyzer_Visualizer`) — fetch a
  platform-injected input and forward it to *their own* backend API. **This doc covers this
  pattern**: one `Analyzer` subclass + one tiny tool function. Everything else (MCP protocol,
  `/mcp/` transport, bearer auth, the streaming/SSRF-guarded log fetch) is shared.
- **Self-contained tools** that don't take a log or forward to a backend (e.g. an EoS date
  lookup, a snippet generator) — just add a plain `@mcp.tool async def …` in `server.py` with
  typed inputs and a text return. Skip the `Analyzer` class entirely; it's only for the
  forwarding pattern below.

The rest of this guide is the forwarding pattern. It's deliberate that **each backend tool has a
different API** (Log Visualizer is a Flask multipart API; the next might be JSON-RPC, a queue,
whatever) — the `Analyzer` interface hides that difference behind two methods.

---

## The contract — `analyzers/base.Analyzer`

```python
class Analyzer(abc.ABC):
    name: str                 # MCP tool name the agent calls
    log_field: str = "source_url"   # the input field the PLATFORM injects the signed log URL into

    def description(self) -> str: ...
    async def analyze(self, *, log_bytes: bytes, filename: str, question: str) -> str: ...
```

- **`name`** — the tool name (a clear verb-y noun, no spaces). One tool = one unit of intent.
- **`log_field`** — the input field the partner wires their signed-URL injector to. Usually
  `source_url`; keep it unless you have a reason.
- **`description()`** — what the agent reads in `list_tools` to decide *whether and how* to
  call the tool. Be specific: which logs it handles, what it returns, and that the platform
  supplies the log (the model must not fill `source_url`).
- **`analyze(...)`** — receives the already-fetched log bytes + filename + the user's question
  (`""` if none). Forward to your backend, reduce the reply to **one text string**, return it.
  On a handled error return a short explanatory string; don't raise raw stack traces.

---

## Step 1 — write the analyzer

`analyzers/fortisiem.py` (illustrative — a JSON API instead of multipart):

```python
import httpx
from .base import Analyzer

DESCRIPTION = (
    "Correlate a FortiSIEM event export and summarize incidents. The platform supplies the "
    "log via the injected `source_url` field — do not fill it. Pass the user's ask in `question`."
)

class FortiSIEMAnalyzer(Analyzer):
    name = "FortiSIEM_Incident_Summarizer"
    log_field = "source_url"

    def __init__(self, api_base: str, timeout: float = 300.0):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def description(self) -> str:
        return DESCRIPTION

    async def analyze(self, *, log_bytes: bytes, filename: str, question: str) -> str:
        # …forward to THIS analyzer's own API however it expects (JSON here, multipart there)…
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                f"{self.api_base}/summarize",
                json={"log": log_bytes.decode("utf-8", "replace"), "question": question},
            )
        if r.status_code >= 400:
            return f"⚠️ FortiSIEM analyzer error (HTTP {r.status_code})."
        data = r.json()
        # …fold `data` into one text report (reduce, don't dump)…
        return data.get("summary", "No summary produced.")
```

**Tip:** put the "JSON → one text report" formatting in a private `_format_report(data)`
function, exactly like `analyzers/logv.py::_format_report`, so it's testable on its own.

## Step 2 — register the tool in `server.py`

```python
from analyzers.fortisiem import FortiSIEMAnalyzer

SIEM = FortiSIEMAnalyzer(api_base=os.getenv("FORTISIEM_API_BASE", "http://…"))

@mcp.tool(name=SIEM.name, description=SIEM.description())
async def fortisiem_incident_summarizer(
    source_url: Annotated[str, Field(description="Signed URL — INJECTED BY THE PLATFORM.")],
    question: Annotated[str, Field(description="The user's question. Optional.")] = "",
) -> str:
    try:
        log_bytes, filename = await fetch_log(source_url)   # shared helper — reuse it
    except Exception as e:
        return f"⚠️ Could not fetch the log: {e}"
    return await SIEM.analyze(log_bytes=log_bytes, filename=filename, question=question)
```

That's it — the shared `fetch_log`, auth, and `/mcp/` transport all apply automatically.

Register the tool with the shared helper so `log_field` is checked against the tool signature:

```python
register(SIEM, fortisiem_incident_summarizer)   # asserts SIEM.log_field is a real parameter
```

> If your analyzer's injected field is **not** `source_url`, set `log_field` on the class **and**
> name the tool's matching parameter the same — `server.register()` fails fast at startup if they
> drift. Tell the partner that field name at go-live.

## Step 3 — configure & test

1. Add any backend URL/token to `.env.example` and read it via `os.getenv` in `server.py`.
2. `python server.py`, then exercise it with `client_test.py`. The client is generic — it just
   needs the tool name; adapt the `call_tool(...)` name/args if your inputs differ, or test the
   raw call:
   ```python
   res = await client.call_tool("FortiSIEM_Incident_Summarizer",
                                {"source_url": url, "question": "..."})
   ```

## Step 4 — go live

Send the partner (per the starter kit): the server URL (`…/mcp/`), the bearer token, and — for
**each** log-consuming tool — its **name** and its **injected-URL field name**
(e.g. `FortiSIEM_Incident_Summarizer` → `source_url`). They enable it per agent.

---

## Conventions & gotchas

- **One tool = one intent.** Don't build a mega "do_anything" tool; add another analyzer.
- **Reduce, don't dump.** The agent reads your text into its context — keep it bounded. The
  backend should do the heavy reduction; the analyzer just formats.
- **Scrub internal names.** If your backend's text leaks an internal model/vendor name you
  don't want the agent to echo, strip it (see `analyzers/logv._scrub`).
- **Return links + iframe URLs as text**, and *tell the agent to render the iframe* — the agent
  can't see JSON structure, only your text.
- **Stateless & prompt.** Fetch the signed URL immediately (it expires), don't persist the log,
  and don't hold per-call state.
- **Errors are text, not tracebacks.** Catch backend/network errors and return a short message.
- **Let the backend classify.** Don't pre-judge the log type in the MCP layer; forward it and
  surface whatever the backend says (including "not supported yet"), like `LogVAnalyzer` does.
