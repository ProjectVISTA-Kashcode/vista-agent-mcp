#!/usr/bin/env python3
"""
client_test.py — end-to-end MCP client for VISTA-MCP.

Simulates exactly what the partner agent does, minus their infra:
  1. Serves the sample log over a throwaway HTTP server (stands in for the partner's
     short-lived *signed URL*). Binds all interfaces and advertises the machine's LAN IP
     so a REMOTE MCP server (e.g. https://vista.fortinet.com/mcp/) can fetch it — a
     loopback 127.0.0.1 URL is only reachable when the server runs on this same box.
  2. Connects to the VISTA-MCP server over MCP (HTTP transport) with the bearer token.
  3. Calls `list_tools` and prints each tool EXACTLY as an MCP client/LLM receives it
     (full description, full input schema, output schema, annotations, token cost),
     then flags tools the model cannot tell apart. Then calls the tool.

Config resolution (highest precedence first):
    CLI flags  --url / --token / --host-ip
    shell env  MCP_URL / MCP_AUTH_TOKEN / LOG_ADVERTISE_HOST
    files      client.env, then .env  (loaded here; do NOT override an already-set env var)
    defaults   http://127.0.0.1:8100/mcp/ , gp9$_I38y3_ , auto-detected LAN IP

Run (server must be reachable):
    source client.env && python client_test.py                 # uses client.env config
    python client_test.py --url https://vista.fortinet.com/mcp --token 'gp9$_I38y3_'
    # NOTE: no trailing slash on the proxied prod URL — `/mcp/` 307-redirects to `/mcp` and the
    # redirect drops the Authorization header (→ 401). Local/LAN URLs work either way.
    python client_test.py --no-question                        # analyze + visualize only
    python client_test.py --host-ip 203.0.113.7                # advertise a public IP for the log
    python client_test.py --list-only                          # just auth + the tool catalog
    python client_test.py --list-only --raw                    # + raw JSON per tool

The token MUST match the server's MCP_AUTH_TOKEN. On a 401 the client prints the (masked)
token it sent so you can compare. Single-quote tokens containing `$` in the shell.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import os
import socket
import textwrap
import threading
from collections import defaultdict

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

# Load client.env / .env WITHOUT clobbering anything already exported (shell wins). This lets
# `python client_test.py` pick up the token/URL from the files, while an explicit `export` or a
# --flag still takes precedence.
try:
    from dotenv import load_dotenv

    _HERE = os.path.dirname(os.path.abspath(__file__))
    for _f in (".env", "client.env"):  # client.env last so it wins between the two files
        _p = os.path.join(_HERE, _f)
        if os.path.exists(_p):
            load_dotenv(_p, override=False)
except Exception:  # noqa: BLE001 — python-dotenv optional; env/flags still work without it
    pass

DEFAULT_MCP_URL = "http://127.0.0.1:8100/mcp/"
DEFAULT_TOKEN = "gp9$_I38y3_"
DEFAULT_FILE = "test_data/DEMO_LOG_VISUALIZER_SDWAN.log"
DEFAULT_QUESTION = "What SLA failures happened on the Austin healthcheck?"


def _detect_lan_ip() -> str:
    """Best-effort primary LAN IP (the source address the OS would use to reach the internet).
    No packets are actually sent. Falls back to 127.0.0.1 if detection fails."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def _mask(token: str) -> str:
    """Show enough of the token to compare against the server without printing the secret."""
    if not token:
        return "(empty)"
    if len(token) <= 4:
        return "*" * len(token) + f"  (len {len(token)})"
    return f"{token[:2]}…{token[-2:]}  (len {len(token)})"


def serve_directory(directory: str) -> http.server.ThreadingHTTPServer:
    """Serve `directory` over HTTP on an ephemeral port, bound to ALL interfaces (0.0.0.0) so a
    remote MCP server can reach it. Background thread. Stands in for the partner's signed URL."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _result_text(res) -> str:
    """Extract the tool's text from a fastmcp CallToolResult across versions."""
    data = getattr(res, "data", None)
    if isinstance(data, str):  # includes "" — an empty report is still the report, not a repr
        return data
    blocks = getattr(res, "content", None) or []
    text = "\n".join(getattr(b, "text", "") for b in blocks if getattr(b, "text", None))
    return text or str(res)


# ── Tool catalog rendering ────────────────────────────────────────────────────
# What an MCP client (and the LLM behind it) actually receives per tool is: name,
# description, and inputSchema — nothing else. Whether the model picks the RIGHT
# tool is decided entirely by that text, so this prints it verbatim and
# untruncated rather than a one-line preview.
WIDTH = 78


def _fmt_type(spec) -> str:
    """Human-readable type for one JSON-Schema property."""
    if not isinstance(spec, dict):
        return "any"
    if "enum" in spec:
        return " | ".join(json.dumps(v) for v in spec["enum"])
    if "anyOf" in spec:
        return " | ".join(_fmt_type(s) for s in spec["anyOf"])
    kind = spec.get("type", "any")
    if kind == "array":
        return f"array<{_fmt_type(spec.get('items') or {})}>"
    return kind


def _wrap(text: str, indent: str) -> str:
    """Wrap to WIDTH, preserving the author's paragraph breaks."""
    out: list[str] = []
    for para in (text or "").split("\n"):
        if not para.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(para, width=max(20, WIDTH - len(indent))))
    return "\n".join(indent + line for line in out)


def _approx_tokens(text: str) -> int:
    """Rough ~4-chars-per-token estimate — enough to compare tools, not exact."""
    return max(1, len(text) // 4)


def _schema_lines(schema: dict) -> list[str]:
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    if not props:
        return ["     (no parameters)"]
    lines: list[str] = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        flag = "REQUIRED" if name in required else "optional"
        head = f"     • {name}  ({_fmt_type(spec)})  {flag}"
        if "default" in spec:
            head += f"  default={spec['default']!r}"
        lines.append(head)
        desc = (spec.get("description") or "").strip()
        if desc:
            # split so the caller can prefix EVERY physical line with the gutter
            lines.extend(_wrap(desc, "         ").split("\n"))
    return lines


def print_tool_catalog(tools, raw: bool = False) -> None:
    """Print every tool the way an MCP client sees it, then flag routing ambiguity."""
    print("=" * WIDTH)
    print(f"list_tools → {len(tools)} tool(s)")
    print("Everything below is the ENTIRE basis a model has for choosing a tool.")
    print("=" * WIDTH)

    total = 0
    for i, t in enumerate(tools, 1):
        desc = t.description or ""
        schema = t.inputSchema or {}
        cost = _approx_tokens(t.name + desc + json.dumps(schema))
        total += cost

        print(f"\n┌─[{i}] {t.name}")
        if getattr(t, "title", None):
            print(f"│   title    : {t.title}")
        ann = getattr(t, "annotations", None)
        if ann is not None:
            hints = ann.model_dump(exclude_none=True)
            print(f"│   hints    : {hints or '(none)'}")
        meta = getattr(t, "meta", None)
        if meta:
            print(f"│   meta     : {json.dumps(meta)[:160]}")
        print(f"│   ~tokens  : {cost}   (context cost of exposing this tool)")
        print("│")
        print("│   DESCRIPTION — verbatim; this is the routing signal:")
        if desc.strip():
            print(_wrap(desc, "│     "))
        else:
            print("│     ⚠️  EMPTY — the model has only the tool NAME to go on.")
        print("│")
        print("│   INPUT SCHEMA:")
        for line in _schema_lines(schema):
            print("│" + line)
        out_schema = getattr(t, "outputSchema", None)
        if out_schema:
            print("│")
            print(f"│   OUTPUT SCHEMA: {json.dumps(out_schema)[:280]}")
        print("└" + "─" * (WIDTH - 1))

        if raw:
            payload = t.model_dump(exclude_none=True, by_alias=True)
            print(textwrap.indent(json.dumps(payload, indent=2, ensure_ascii=False), "    "))

    print(f"\n≈{total} tokens of context consumed by this catalog.")
    _report_ambiguity(tools)


def _report_ambiguity(tools) -> None:
    """Flag tools a model cannot tell apart.

    Every VISTA tool is registered from the same factory, so they share one input
    schema by construction — the per-tool description in TOOL_ENABLEMENT is the
    only differentiator. If two descriptions match, routing between them is a
    coin flip no prompt engineering downstream can fix.
    """
    empty = [t.name for t in tools if not (t.description or "").strip()]
    by_desc: dict[str, list[str]] = defaultdict(list)
    by_schema: dict[str, list[str]] = defaultdict(list)
    for t in tools:
        by_desc[" ".join((t.description or "").split()).lower()].append(t.name)
        by_schema[json.dumps(t.inputSchema or {}, sort_keys=True)].append(t.name)

    dup_desc = [n for n in by_desc.values() if len(n) > 1]
    dup_schema = [n for n in by_schema.values() if len(n) > 1]
    if not (empty or dup_desc or dup_schema):
        return

    print("\n" + "!" * WIDTH)
    print("ROUTING AMBIGUITY — what a model cannot distinguish")
    for names in empty:
        print(f"  ✖ no description: {names}")
    for names in dup_desc:
        print(f"  ✖ IDENTICAL descriptions: {', '.join(names)}")
        print("    Nothing distinguishes these; the model picks arbitrarily.")
        print("    Fix: give each a distinct description in config/tool_enablement.json.")
    for names in dup_schema:
        if names not in dup_desc:
            print(f"  • identical input schemas: {', '.join(names)}")
            print("    Expected here (shared factory) — the description must carry the distinction.")
    print("!" * WIDTH)


async def main(args: argparse.Namespace) -> None:
    mcp_url = args.url or os.getenv("MCP_URL", DEFAULT_MCP_URL)
    token = args.token or os.getenv("MCP_AUTH_TOKEN", DEFAULT_TOKEN)
    # IP the MCP server will use to fetch the log: explicit flag → env → auto-detected LAN IP.
    advertise_host = args.host_ip or os.getenv("LOG_ADVERTISE_HOST") or _detect_lan_ip()

    file_path = os.path.abspath(args.file)
    if not os.path.exists(file_path):
        raise SystemExit(f"log file not found: {file_path}")
    directory, fname = os.path.dirname(file_path), os.path.basename(file_path)

    httpd = serve_directory(directory)
    port = httpd.server_address[1]
    source_url = f"http://{advertise_host}:{port}/{fname}"

    print(f"→ MCP server : {mcp_url}")
    print(f"→ auth token : {_mask(token)}")
    print(f"→ log served : {source_url}  ({os.path.getsize(file_path):,} bytes)")
    if advertise_host in ("127.0.0.1", "localhost"):
        print("  ⚠️  advertising loopback — a REMOTE MCP server cannot fetch this. Pass "
              "--host-ip <ip reachable by the server>.")
    print()

    transport = StreamableHttpTransport(mcp_url, auth=token)
    try:
        try:
            client_cm = Client(transport)
            async with client_cm as client:
                tools = await client.list_tools()
                print_tool_catalog(tools, raw=args.raw)

                if args.list_only:
                    return

                question = "" if args.no_question else args.question
                call_args = {"source_url": source_url}
                if question:
                    call_args["question"] = question
                print(f"\ncall log_v_internal_only(question={question!r}) …\n")
                res = await client.call_tool("log_v_internal_only", call_args)

                print("┌" + "─" * 76 + "┐")
                print("│ TOOL RESULT (the single text block the agent reasons over):")
                print("└" + "─" * 76 + "┘")
                print(_result_text(res))
        except Exception as e:  # noqa: BLE001 — turn the raw stack into an actionable message
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                slash_hint = ""
                if mcp_url.rstrip().endswith("/"):
                    no_slash = mcp_url.rstrip().rstrip("/")
                    slash_hint = (
                        "\n  ⚠️  Your URL ends with '/'. Behind a reverse proxy, `/mcp/` can 307-redirect "
                        "to `/mcp`\n      and the redirect DROPS the Authorization header → this 401. "
                        f"Try WITHOUT the slash:\n        --url {no_slash}"
                    )
                raise SystemExit(
                    "\n✗ 401 Unauthorized — the server rejected the bearer token."
                    f"\n  client sent : {_mask(token)}\n  server      : {mcp_url}{slash_hint}"
                    "\n  Also check MCP_AUTH_TOKEN matches the value the server was started with "
                    "(single-quote it in the\n  shell if it contains `$`; a stale `export` can shadow "
                    "client.env)."
                )
            raise
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=DEFAULT_FILE, help="log file to serve as the injected URL")
    p.add_argument("--question", default=DEFAULT_QUESTION, help="natural-language question")
    p.add_argument("--no-question", action="store_true", help="omit the question (analyze+visualize)")
    p.add_argument("--list-only", action="store_true", help="just auth + list tools and exit")
    p.add_argument("--raw", action="store_true",
                   help="also dump each tool's raw JSON exactly as the MCP client receives it")
    p.add_argument("--url", default=None, help="MCP server URL (overrides MCP_URL / client.env)")
    p.add_argument("--token", default=None, help="bearer token (overrides MCP_AUTH_TOKEN / client.env)")
    p.add_argument("--host-ip", default=None,
                   help="IP the MCP server can reach the log server at (default: auto-detected LAN IP)")
    asyncio.run(main(p.parse_args()))
