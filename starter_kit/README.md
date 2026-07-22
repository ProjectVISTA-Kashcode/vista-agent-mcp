# Build an MCP tool for our agents

This is a **self-contained starter kit**. You build tools as a standalone **MCP
server** in your own repo and stack; our agent connects to it over the network
and calls your tools. **You never need our source code, our data, or repo
access** — the integration is a network protocol plus a small JSON contract.

Think of it as writing a plugin: your server, your language, your deploy
pipeline. We register your server on our side and choose which of your tools our
agents may call.

---

## 1. What you build

A server that speaks the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) and exposes one or more **tools**. Each tool is a function with a typed
input schema and a text result. `server.py` here is a working example exposing a
single `filter_log` tool — clone this folder and extend it.

```sh
pip install -r requirements.txt
python server.py         # serves at  http://0.0.0.0:8100/mcp/
```

## 2. How our agent uses it

1. Our agent connects to your server and calls **`list_tools`** — it reads each
   tool's **name, description, and input schema**. Write these well: the model
   decides *whether and how* to call your tool from that text alone.
2. When it decides to use a tool, it calls it with JSON arguments and reads the
   text you return as an observation in its reasoning.

So: clear names, tight descriptions, small typed inputs, useful text output.

## 3. The one special input: the log URL

For a tool that processes an uploaded log, **we hand you the log — you don't ask
the user for it.** Declare an input field (the example uses `source_url`) and
**we inject a short-lived, signed URL** to the log into that field automatically.
The model never fills it.

- **Fetch it promptly** — the URL expires in minutes.
- **Stream it** — logs can be large; don't assume it's small (see `_MAX_LOG_BYTES`).
- **Don't store it or the log** — fetch, process, return.

You tell us which field name should receive the URL; we wire it on our side.

## 4. Contract for a good tool

- **One tool = one unit of intent** with a clear verb (`filter_log`,
  `enrich_iocs`), not a mega "do_anything".
- **Inputs**: a few typed, well-described fields. The model fills all of them
  *except* the injected log URL.
- **Output**: return **text** the agent can reason over. Keep it bounded — reduce,
  don't dump (the example caps returned lines).
- **Errors**: raise a clear exception or return a short explanatory string; don't
  return internal stack traces.
- **Stateless** per call where possible.

## 5. Auth

Our agent sends an `Authorization: Bearer <token>` header we agree on. Validate
it on your server and reject anything else. FastMCP supports auth providers; for
a quick start, check the header in middleware and 401 on mismatch. **Don't ship
an unauthenticated server** — anything that can reach it could call your tools.

## 6. What to send us to go live

Once your server runs and is reachable from our backend:

1. **Server URL** — e.g. `https://tools.yourhost.example/mcp/` (include the path).
2. **Auth token** — the bearer token we should send (share it securely).
3. **For each tool that consumes a log**: the tool **name** and the **input field
   name** that should receive the injected log URL (e.g. `filter_log` →
   `source_url`).

We then register your server and enable the specific tools for the relevant
agents. We can revoke or rotate the token at any time; you can add/change tools
and we'll pick them up.

## 7. Test locally without us

You don't need our backend to develop. Run the server and call a tool with a
plain MCP client, or point `source_url` at any URL that returns a log (e.g. a
local `python -m http.server` serving a sample log file) and exercise
`filter_log` directly. When it behaves the way you want against a real log URL,
it'll behave the same when our agent calls it.

---

**Files**
- `server.py` — the example MCP server (one `filter_log` tool). Extend this.
- `requirements.txt` — `fastmcp`, `httpx`.
