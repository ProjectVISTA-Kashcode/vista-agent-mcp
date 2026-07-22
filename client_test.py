#!/usr/bin/env python3
"""
client_test.py — end-to-end MCP client for VISTA-MCP.

Simulates exactly what the partner agent does, minus their infra:
  1. Serves the sample log over a throwaway local HTTP server (stands in for the
     partner's short-lived *signed URL*).
  2. Connects to the VISTA-MCP server over MCP (HTTP transport) with the bearer token.
  3. Calls `list_tools` (prints tool name / description / input schema).
  4. Calls `Log_Analyzer_Visualizer` with `{source_url, question}` and prints the single
     text report the agent would reason over.

Run (server must be up — `python server.py`):
    python client_test.py                                  # default question
    python client_test.py --question "SLA failures for Austin"
    python client_test.py --no-question                    # analyze + visualize only
    python client_test.py --file test_data/sdwan-small.log
    python client_test.py --list-only

Env: MCP_URL (default http://127.0.0.1:8100/mcp/), MCP_AUTH_TOKEN (default vista-dev-token).
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import os
import threading

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8100/mcp/")
TOKEN = os.getenv("MCP_AUTH_TOKEN", "vista-dev-token")
DEFAULT_FILE = "test_data/DEMO_LOG_VISUALIZER_SDWAN.log"
DEFAULT_QUESTION = "What SLA failures happened on the Austin healthcheck?"


def serve_directory(directory: str) -> tuple[str, http.server.ThreadingHTTPServer]:
    """Serve `directory` over HTTP on an ephemeral localhost port (background thread).
    Returns (base_url, server). Stands in for the partner's signed log URL."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def _result_text(res) -> str:
    """Extract the tool's text from a fastmcp CallToolResult across versions."""
    data = getattr(res, "data", None)
    if isinstance(data, str):  # includes "" — an empty report is still the report, not a repr
        return data
    blocks = getattr(res, "content", None) or []
    text = "\n".join(getattr(b, "text", "") for b in blocks if getattr(b, "text", None))
    return text or str(res)


async def main(args: argparse.Namespace) -> None:
    file_path = os.path.abspath(args.file)
    if not os.path.exists(file_path):
        raise SystemExit(f"log file not found: {file_path}")
    directory, fname = os.path.dirname(file_path), os.path.basename(file_path)

    base, httpd = serve_directory(directory)
    # If the MCP server runs on another host, 127.0.0.1 won't be reachable from it —
    # override with --host-ip <ip reachable by the server>.
    source_url = f"{base.replace('127.0.0.1', args.host_ip)}/{fname}"

    print(f"→ MCP server : {MCP_URL}")
    print(f"→ log served : {source_url}  ({os.path.getsize(file_path):,} bytes)\n")

    transport = StreamableHttpTransport(MCP_URL, auth=TOKEN)
    try:
        async with Client(transport) as client:
            tools = await client.list_tools()
            print("=" * 78)
            print(f"list_tools → {len(tools)} tool(s):")
            for t in tools:
                print(f"  • {t.name}")
                print(f"      desc  : {(t.description or '').splitlines()[0][:100]}")
                props = list((t.inputSchema or {}).get("properties", {}).keys())
                required = (t.inputSchema or {}).get("required", [])
                print(f"      inputs: {props}  (required: {required})")
            print("=" * 78)

            if args.list_only:
                return

            question = "" if args.no_question else args.question
            call_args = {"source_url": source_url}
            if question:
                call_args["question"] = question
            print(f"\ncall Log_Analyzer_Visualizer(question={question!r}) …\n")
            res = await client.call_tool("Log_Analyzer_Visualizer", call_args)

            print("┌" + "─" * 76 + "┐")
            print("│ TOOL RESULT (the single text block the agent reasons over):")
            print("└" + "─" * 76 + "┘")
            print(_result_text(res))
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=DEFAULT_FILE, help="log file to serve as the injected URL")
    p.add_argument("--question", default=DEFAULT_QUESTION, help="natural-language question")
    p.add_argument("--no-question", action="store_true", help="omit the question (analyze+visualize)")
    p.add_argument("--list-only", action="store_true", help="just list tools and exit")
    p.add_argument("--host-ip", default="127.0.0.1", help="IP the MCP server can reach the file server at")
    asyncio.run(main(p.parse_args()))
