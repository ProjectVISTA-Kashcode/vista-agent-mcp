"""
VISTA-MCP — Model Context Protocol server (gateway to VISTA tools)
==================================================================

A partner agent connects over MCP (HTTP transport, path **/mcp/**), calls `list_tools`, and
invokes a tool. VISTA-MCP hosts a growing catalog of Fortinet VISTA tools for FortiOS/FortiGate
engineering and support — log analysis & visualization today; config/script generation,
debug-command helpers, End-of-Support & lifecycle lookups, and more over time. Tools are
independent and varied: each declares its own inputs and returns its own text — **not all take
a log or return a visualization**.

Some tools (like the log analyzer) follow a **proxy** pattern — fetch a platform-injected input,
forward it to that tool's own backend API, and return one text report; that pattern lives in
`analyzers/` (the `Analyzer` base + one subclass per backend). Tools that don't forward to a
backend can be added as plain `@mcp.tool` functions.

Tools today:
  * `Log_Analyzer_Visualizer` → Log Visualizer "AgentAssist" API (FortiGate SD-WAN today).

Adding a backend-backed tool = one `Analyzer` subclass + one small tool function here
(see docs/ADDING_ANALYZERS.md). The MCP protocol, auth, log-fetch, and SSRF guard are shared.

Run:
    pip install -r requirements.txt
    python server.py            # serves at http://0.0.0.0:8100/mcp/

Config (env; a .env file is auto-loaded if python-dotenv is installed):
    MCP_HOST                 default 0.0.0.0
    MCP_PORT                 default 8100
    MCP_AUTH_TOKEN           shared bearer token the partner agent must send. REQUIRED —
                             the server refuses to start with an empty or default token
                             unless MCP_ALLOW_INSECURE=1 (local dev only).
    MCP_ALLOW_INSECURE       '1' to permit the dev token / no-auth (local dev only)
    MCP_ALLOW_PRIVATE_FETCH  '1' to allow fetching private/loopback URLs (local testing only;
                             production signed URLs are public, so keep this off)
    MAX_LOG_BYTES            default 52428800 (50 MB) — streaming fetch cap
    MCP_LOG_LEVEL            default INFO (DEBUG previews the report text)
    LOGV_API_BASE            Log Visualizer AgentAssist base
                             default http://127.0.0.1:8802/logVisualizer/api/agent_assist
    LOGV_VIEW_BASE           SPA base for the returned session links/iframe
                             default https://vista.fortinet.com/logVisualizer
"""
from __future__ import annotations

import inspect
import ipaddress
import os
import socket
import time
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from pydantic import Field

try:  # optional: auto-load a .env file if python-dotenv is present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

import vlog
from analyzers import LogVAnalyzer

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8100"))
AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "vista-dev-token")
ALLOW_INSECURE = _flag("MCP_ALLOW_INSECURE")
ALLOW_PRIVATE_FETCH = _flag("MCP_ALLOW_PRIVATE_FETCH")
MAX_LOG_BYTES = int(os.getenv("MAX_LOG_BYTES", str(50 * 1024 * 1024)))
LOGV_API_BASE = os.getenv(
    "LOGV_API_BASE", "http://127.0.0.1:8802/logVisualizer/api/agent_assist"
)
LOGV_VIEW_BASE = os.getenv("LOGV_VIEW_BASE", "https://vista.fortinet.com/logVisualizer")
ALLOWED_EXT = {"log", "txt", "csv", "gz"}

# ORB troubleshooting API — after the LogV analyzer gets the analysis, it asks ORB for
# remediation steps relevant to it and folds them into the report (fail-open). Disable with
# ORB_ENABLED=0.
ORB_ENABLED = os.getenv("ORB_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
ORB_ASK_URL = os.getenv("ORB_ASK_URL", "https://vista.fortinet.com/orb/api/ask")
ORB_USERNAME = os.getenv("ORB_USERNAME", "logV_mcp_call")
ORB_TIMEOUT = float(os.getenv("ORB_TIMEOUT", "180"))  # ORB is deep-research; allow ~3 min

# --------------------------------------------------------------------------- #
# Auth — a pre-shared static bearer token. Fail CLOSED: refuse to start with an empty
# or the built-in default token unless MCP_ALLOW_INSECURE=1 (the starter kit is explicit:
# "Don't ship an unauthenticated server").
# --------------------------------------------------------------------------- #
_weak_token = (not AUTH_TOKEN) or (AUTH_TOKEN == "vista-dev-token")
if _weak_token and not ALLOW_INSECURE:
    raise SystemExit(
        "VISTA-MCP refuses to start: set MCP_AUTH_TOKEN to a strong, non-default value "
        "(empty or the built-in 'vista-dev-token' is not allowed). "
        "For local dev only, set MCP_ALLOW_INSECURE=1."
    )
auth = (
    StaticTokenVerifier({AUTH_TOKEN: {"client_id": "vista-agent", "scopes": ["analyze"]}})
    if AUTH_TOKEN
    else None
)

mcp = FastMCP(
    "VISTA-MCP",
    instructions=(
        "VISTA-MCP is a gateway to a growing catalog of Fortinet VISTA tools for FortiOS / "
        "FortiGate engineering and support — for example log analysis & visualization, and "
        " configuration/script generation, debug-command helpers, End-of-Support, debug analyzers "
        "and lifecycle lookups, and more. The tools are independent and varied: each has its "
        "own inputs and its own output, and not all of them take a log file or return a "
        "visualization. Read each tool's name and description in list_tools and call the one "
        "whose stated purpose matches the user's task — don't assume a single shape across tools."
    ),
    auth=auth,
)

# One analyzer instance per backend (add more below to expose more tools).
LOGV = LogVAnalyzer(
    api_base=LOGV_API_BASE,
    view_base=LOGV_VIEW_BASE,
    orb_enabled=ORB_ENABLED,
    orb_url=ORB_ASK_URL,
    orb_username=ORB_USERNAME,
    orb_timeout=ORB_TIMEOUT,
)


# --------------------------------------------------------------------------- #
# SSRF guard — the injected URL is fetched server-side; reject non-public targets so a
# redirect or a misfilled URL can't reach internal hosts (metadata, the LogV backend, …).
# Enforced on the initial request AND every redirect hop via an httpx request hook.
# --------------------------------------------------------------------------- #
def _assert_public_host(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme '{p.scheme or '?'}'")
    host = p.hostname
    if not host:
        raise ValueError("URL has no host")
    if ALLOW_PRIVATE_FETCH:
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("cannot resolve URL host") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise ValueError("refusing to fetch a non-public (internal) address")


async def _guard_request(request: httpx.Request) -> None:
    _assert_public_host(str(request.url))


# --------------------------------------------------------------------------- #
# Shared log fetch — the platform injects a short-lived signed URL; fetch it now, stream
# it (logs can be large), cap it, don't store it. The signed query is never logged.
# --------------------------------------------------------------------------- #
async def fetch_log(source_url: str) -> tuple[bytes, str]:
    """Download the injected log URL (SSRF-guarded, streaming, size-capped). Returns (bytes, filename)."""
    t0 = time.time()
    vlog.log(f"fetch: GET {vlog.redact_url(source_url)} (streaming, cap {MAX_LOG_BYTES:,}B)")
    timeout = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
    chunks: list[bytes] = []
    total = 0
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, event_hooks={"request": [_guard_request]}
    ) as client:
        async with client.stream("GET", source_url) as resp:
            vlog.log(f"fetch: HTTP {resp.status_code} · content-type={resp.headers.get('content-type','?')}")
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_LOG_BYTES:
                    vlog.log(f"fetch: ABORTED — exceeded {MAX_LOG_BYTES:,}B cap", vlog.WARNING)
                    raise ValueError(f"log exceeds the {MAX_LOG_BYTES}-byte limit this tool will process")
                chunks.append(chunk)
    raw = b"".join(chunks)
    # derive a filename with a known extension so the backend can sniff the format
    name = os.path.basename(urlparse(source_url).path) or "logfile.log"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_EXT:
        vlog.log("fetch: URL had no known log extension → using filename 'logfile.log'")
        name = "logfile.log"
    vlog.log(f"fetch: done — {len(raw):,} bytes, filename='{name}' in {time.time()-t0:.2f}s")
    return raw, name


# --------------------------------------------------------------------------- #
# Tools  (one thin proxy function per analyzer)
# --------------------------------------------------------------------------- #
async def log_analyzer_visualizer(
    source_url: Annotated[
        str,
        Field(
            description=(
                "Signed URL to the uploaded FortiOS/FortiGate log. THE PLATFORM INJECTS "
                "THIS automatically — the model must not fill or invent it."
            )
        ),
    ],
    question: Annotated[
        str,
        Field(
            description=(
                "The user's natural-language question about the log (e.g. 'SLA failures on "
                "the Austin healthcheck between 12:09 and 12:11'). Optional — omit for a "
                "general analysis + visualization."
            )
        ),
    ] = "",
) -> str:
    vlog.new_rid()
    t0 = time.time()
    vlog.log("─" * 70)
    vlog.log(
        f"▶ TOOL CALL  {LOGV.name}  question={vlog.short(question, 160)!r}  "
        f"source_url={vlog.redact_url(source_url)}"
    )
    # --- fetch (leak-safe error handling: never echo the signed URL or a raw stack) ---
    try:
        log_bytes, filename = await fetch_log(source_url)
    except ValueError as e:  # our own guards (scheme/host/size) — messages are URL-free
        vlog.log(f"✖ fetch rejected: {e} [{vlog.redact_url(source_url)}]", vlog.WARNING)
        vlog.log(f"◀ TOOL CALL done (fetch rejected) in {time.time()-t0:.2f}s")
        return f"⚠️ Could not fetch the log: {e}."
    except httpx.HTTPStatusError as e:  # str(e) contains the signed URL — do NOT echo it
        vlog.log(f"✖ fetch failed HTTP {e.response.status_code} for {vlog.redact_url(source_url)}", vlog.ERROR)
        vlog.log(f"◀ TOOL CALL done (fetch error) in {time.time()-t0:.2f}s")
        return f"⚠️ Could not fetch the log (HTTP {e.response.status_code})."
    except Exception as e:  # noqa: BLE001  # connect/timeout/etc — type only, redacted URL
        vlog.log(f"✖ fetch failed ({type(e).__name__}) for {vlog.redact_url(source_url)}", vlog.ERROR)
        vlog.log(f"◀ TOOL CALL done (fetch error) in {time.time()-t0:.2f}s")
        return f"⚠️ Could not fetch the log ({type(e).__name__})."

    # --- analyze (wrapped so a formatting/backend bug never escapes as a raw MCP error) ---
    try:
        report = await LOGV.analyze(log_bytes=log_bytes, filename=filename, question=question)
    except Exception as e:  # noqa: BLE001
        vlog.log(f"✖ analyze failed: {type(e).__name__}: {e}", vlog.ERROR)
        report = "⚠️ The Log Analyzer encountered an internal error while processing the log."

    vlog.log(f"◀ TOOL CALL done — returning {len(report):,}-char report in {time.time()-t0:.2f}s")
    vlog.log(f"report preview: {vlog.short(report, 400)}", vlog.DEBUG)
    return report


def register(analyzer, fn) -> None:
    """Register an analyzer's tool, asserting its declared `log_field` (the field the
    platform injects the signed URL into) is actually a parameter of the tool function —
    so `log_field` and the tool signature can't silently drift when adding analyzers."""
    params = inspect.signature(fn).parameters
    if analyzer.log_field not in params:
        raise SystemExit(
            f"{analyzer.name}: declared log_field '{analyzer.log_field}' is not a parameter "
            f"of its tool function {list(params)} — fix the tool signature or log_field."
        )
    mcp.tool(name=analyzer.name, description=analyzer.description())(fn)


register(LOGV, log_analyzer_visualizer)


if __name__ == "__main__":
    vlog.log("=" * 70)
    vlog.log(f"VISTA-MCP starting → http://{HOST}:{PORT}/mcp/")
    vlog.log(f"  tools            : {LOGV.name}")
    vlog.log(f"  auth             : {'Bearer token (StaticTokenVerifier)' if auth else 'DISABLED ⚠️ (MCP_ALLOW_INSECURE)'}")
    vlog.log(f"  private fetch     : {'ALLOWED (dev)' if ALLOW_PRIVATE_FETCH else 'blocked (SSRF guard)'}")
    vlog.log(f"  LOGV_API_BASE    : {LOGV_API_BASE}")
    vlog.log(f"  LOGV_VIEW_BASE   : {LOGV_VIEW_BASE}  (session links + iframe)")
    vlog.log(f"  ORB troubleshoot : {'ON → ' + ORB_ASK_URL if ORB_ENABLED else 'OFF'}"
             + (f'  (timeout {ORB_TIMEOUT:.0f}s)' if ORB_ENABLED else ''))
    vlog.log(f"  MAX_LOG_BYTES    : {MAX_LOG_BYTES:,}")
    vlog.log(f"  log level        : {os.getenv('MCP_LOG_LEVEL', 'INFO')}")
    if _weak_token and ALLOW_INSECURE:
        vlog.log("INSECURE dev mode — weak/no auth token permitted. Do NOT use in production.", vlog.WARNING)
    vlog.log("=" * 70)
    # HTTP transport; the served MCP path is /mcp/  → give partners  http://<host>:<port>/mcp/
    mcp.run(transport="http", host=HOST, port=PORT)
