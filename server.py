"""
VISTA-MCP — Model Context Protocol server (gateway to VISTA tools)
==================================================================

A partner agent connects over MCP (HTTP transport, path **/mcp/**), calls `list_tools`, and
invokes a tool. VISTA-MCP hosts a growing catalog of Fortinet VISTA tools for FortiOS/FortiGate
engineering and support — log analysis & visualization today; config/script generation,
debug-command helpers, End-of-Support & lifecycle lookups, and more over time. Tools are
independent and varied: each declares its own inputs and returns its own text — **not all take
a log or return a visualization**.

Every tool here is a thin entry point into the **config-driven orchestrator**
(`orchestrator/`): fetch the platform-injected input, then run VFR → TOOL_ENABLEMENT →
DISCOVER → **AI CONTROLLER** → analyzers (in parallel) → concatenate → ORB → answer. Tools are
registered from `config/tool_enablement.json`, so adding one is a config edit made in the GUI —
no code and no restart (see docs/how_to_add_analyzer.md).

Tools today:
  * `Log_Analyzer_Visualizer` → Log Visualizer "AgentAssist" API (FortiGate SD-WAN today).

Run:
    pip install -r requirements.txt
    python server.py            # serves at http://0.0.0.0:8100/mcp/  (GUI at /gui)

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
    MCP_FETCH_CA_BUNDLE      PEM bundle of extra CAs to trust on outbound calls (internal PKI)
    MCP_FETCH_INSECURE_TLS_HOSTS
                             comma-separated hosts to skip TLS verification for (`curl -k`,
                             scoped) — for internal hosts whose cert name doesn't match
    MCP_FETCH_INSECURE_TLS   '1' to skip TLS verification for every host (blunt; prefer the list)
    MCP_LOG_LEVEL            default INFO (DEBUG previews the report text)
    LOGV_API_BASE            Log Visualizer AgentAssist base
                             default http://127.0.0.1:8802/logVisualizer/api/agent_assist
    LOGV_VIEW_BASE           SPA base for the returned session links/iframe
                             default https://vista.fortinet.com/logVisualizer
    AI_CONTROLLER_ENABLED    '0' disables the AI Controller (deterministic routing + calls
                             built straight from each analyzer's discovery). Default on.
    AI_CONTROLLER_GEN_URL    AI gateway used for the routing/invocation decision
    AI_CONTROLLER_TIMEOUT    gateway read timeout, seconds (default 60)
    DATABASE_URL             Postgres for durable job history + GUI analytics, e.g.
                             postgresql+psycopg://user:pass@host:5432/usage_logs
                             (unset ⇒ history off; the server runs exactly as before)
    MCP_DB_AUTO_CREATE       '0' to skip CREATE TABLE IF NOT EXISTS (schema managed by hand;
                             see docs/db_setup.md)
"""
from __future__ import annotations

import atexit
import ipaddress
import os
import re
import socket
import time
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.tools import Tool
from pydantic import Field

try:  # optional: auto-load a .env file if python-dotenv is present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

import tlsconf
import vlog
from orchestrator import ai_controller, db, gui, pipeline, tool_enablement

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
    "LOGV_API_BASE", "https://vista.fortinet.com/logVisualizer/api/agent_assist"
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

# --------------------------------------------------------------------------- #
# MCP tools. Each tool maps to a TOOL_ENABLEMENT config (config/tool_enablement.json)
# that lists the analyzers it may use + whether ORB runs at the end. Adding an analyzer
# to a tool is a CONFIG edit — no code here. The tool function just fetches the injected
# log and hands it to the orchestrator pipeline.
# --------------------------------------------------------------------------- #
TOOL_NAME = "Log_Analyzer_Visualizer"
# Fallback description if the tool's config has none. The client-facing description is
# config-driven (config/tool_enablement.json → tools.<name>.description) so it can be edited
# per tool alongside its routing prompt and analyzers — see _tool_description().
TOOL_DESCRIPTION = (
    "Analyze and visualize a FortiOS/FortiGate (or FortiAnalyzer) log file. The platform "
    "supplies the log automatically via the injected `source_url` field — you do NOT ask the "
    "user for the file and you do NOT fill `source_url`. Pass the user's natural-language "
    "question in `question` (optional). Returns one text report: the log analysis and an "
    "interactive dashboard link, plus troubleshooting suggestions and any relevant companion "
    "analyses. It auto-detects the log's event type and routes internally."
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


def _guard_request(relaxed_tls: bool):
    """Build the per-hop request hook. Beyond the SSRF check, when this client runs with TLS
    verification disabled it must stay on hosts we chose to relax — otherwise a redirect from
    a relaxed staging host would silently carry the unverified connection to an arbitrary
    target."""

    async def hook(request: httpx.Request) -> None:
        url = str(request.url)
        _assert_public_host(url)
        if relaxed_tls and not tlsconf.is_relaxed(url):
            raise ValueError(
                f"refusing to follow a redirect to '{tlsconf.host_of(url) or '?'}' with TLS "
                f"verification disabled — add that host to MCP_FETCH_INSECURE_TLS_HOSTS if intended"
            )

    return hook


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
        timeout=timeout,
        follow_redirects=True,
        verify=tlsconf.verify_for(source_url),
        event_hooks={"request": [_guard_request(tlsconf.is_relaxed(source_url))]},
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
# Tools — registered DYNAMICALLY from TOOL_ENABLEMENT config (config/tool_enablement.json).
#
# Every VISTA tool shares the same MCP surface: it receives the platform-injected `source_url`
# (+ optional `question`), fetches the log, and hands it to the orchestrator pipeline keyed by
# the tool's name. So ONE generic function serves any tool, and adding a tool is a CONFIG edit
# (via the GUI) — a new MCP tool + its endpoints appear with no code and no restart.
# --------------------------------------------------------------------------- #
GENERIC_TOOL_DESCRIPTION = (
    "A VISTA analysis tool. The platform supplies the input file automatically via the injected "
    "`source_url` field — do NOT ask the user for the file and do NOT fill `source_url`. Pass the "
    "user's natural-language question in `question` (optional). Returns one text report."
)


async def _run_tool(tool_name: str, source_url: str, question: str) -> str:
    """Fetch the injected log and run the orchestrator pipeline for ``tool_name``.

    Shared by every dynamically-registered tool — the tool's identity is just the ``tool_name``
    passed to the pipeline, which loads that tool's analyzers/routing/ORB from config.
    """
    rid = vlog.new_rid()
    t0 = time.time()
    vlog.log("═" * 72)
    vlog.log(
        f"▶ TOOL CALL  {tool_name}  question={vlog.short(question, 160)!r}  "
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
        # A rejected certificate arrives as a bare ConnectError; name it, or the operator sees
        # only "ConnectError" for what is really a trust-store problem with a one-line fix.
        cert = tlsconf.is_cert_error(e)
        vlog.log(
            f"✖ fetch failed ({type(e).__name__}{', TLS certificate rejected' if cert else ''}) "
            f"for {vlog.redact_url(source_url)}",
            vlog.ERROR,
        )
        if cert:
            vlog.log(f"  ↳ {tlsconf.cert_error_hint(source_url)}", vlog.ERROR)
        vlog.log(f"◀ TOOL CALL done (fetch error) in {time.time()-t0:.2f}s")
        if cert:
            return (
                "⚠️ Could not fetch the log: the server's TLS certificate was rejected. "
                "This is a server-side trust configuration issue, not a problem with the "
                "request — the administrator needs to trust the issuing CA or allow this host."
            )
        return f"⚠️ Could not fetch the log ({type(e).__name__})."

    # --- orchestrate: VFR → TOOL_ENABLEMENT → discover → decide → call analyzers → ORB ---
    # (wrapped so a pipeline bug never escapes as a raw MCP error; the pipeline itself is
    # also fail-safe internally and always returns a string.)
    try:
        report = await pipeline.run(
            tool_name=tool_name, file_bytes=log_bytes, filename=filename,
            question=question, job_id=rid,
        )
    except Exception as e:  # noqa: BLE001
        vlog.log(f"✖ pipeline failed: {type(e).__name__}: {e}", vlog.ERROR)
        report = "⚠️ The analysis pipeline encountered an internal error while processing the log."

    vlog.log(f"◀ TOOL CALL done — returning {len(report):,}-char report in {time.time()-t0:.2f}s")
    vlog.log(f"report preview: {vlog.short(report, 400)}", vlog.DEBUG)
    return report


def _make_tool_fn(tool_name: str):
    """Build a typed MCP tool function bound to ``tool_name`` (all tools share this shape)."""
    async def tool_fn(
        source_url: Annotated[str, Field(description=(
            "Signed URL to the uploaded FortiOS/FortiGate log. THE PLATFORM INJECTS THIS "
            "automatically — the model must not fill or invent it."))],
        question: Annotated[str, Field(description=(
            "The user's natural-language question about the log (optional — omit for a general "
            "analysis + visualization)."))] = "",
    ) -> str:
        return await _run_tool(tool_name, source_url, question)

    tool_fn.__name__ = re.sub(r"\W", "_", tool_name).lower() or "vista_tool"
    return tool_fn


# name -> currently-registered description, so sync can add / remove / refresh tools live.
_REGISTERED: dict[str, str] = {}


def _desc_for(tool_name: str, tc) -> str:
    fallback = TOOL_DESCRIPTION if tool_name == TOOL_NAME else GENERIC_TOOL_DESCRIPTION
    return (tc.description.strip() if tc and tc.description.strip() else fallback)


def sync_tools(cfg: dict | None = None) -> None:
    """Reconcile the live MCP tool set with TOOL_ENABLEMENT config.

    Called once at startup and again after every GUI config save (via
    ``tool_enablement.on_change``). Adding a tool in the GUI therefore exposes a real MCP tool
    (name + endpoints) with **no restart and no code**; removing one unregisters it; editing a
    tool's description refreshes it. (Analyzer/routing/ORB edits need no re-registration — the
    pipeline reads them from config per call.)
    """
    cfg = cfg if cfg is not None else tool_enablement.load()
    desired = {name: _desc_for(name, tc) for name, tc in cfg.items()}

    for name, desc in desired.items():
        if _REGISTERED.get(name) == desc:
            continue
        if name in _REGISTERED:                       # description changed → refresh
            try:
                mcp.remove_tool(name)
            except Exception:  # noqa: BLE001
                pass
        mcp.add_tool(Tool.from_function(_make_tool_fn(name), name=name, description=desc))
        _REGISTERED[name] = desc
        vlog.log(f"  ⊕ tool registered: {name}")

    for name in list(_REGISTERED):                    # dropped from config → unregister
        if name not in desired:
            try:
                mcp.remove_tool(name)
            except Exception:  # noqa: BLE001
                pass
            _REGISTERED.pop(name, None)
            vlog.log(f"  ⊖ tool unregistered: {name}")


# Register every configured tool now, and keep the live tool set in sync with GUI config edits.
sync_tools()
tool_enablement.on_change(sync_tools)

# Durable job history (Postgres). Fail-open: if the database is unreachable the server runs
# exactly as before — the live GUI still works, only history/analytics are unavailable.
db.init()
atexit.register(db.shutdown)

# Operator console — live flow + dashboard analytics + durable history + TOOL_ENABLEMENT editor,
# served at BOTH /gui and /mcp/gui. Shares the in-memory job registry with the pipeline.
gui.register(mcp)


if __name__ == "__main__":
    vlog.log("=" * 70)
    vlog.log(f"VISTA-MCP starting → http://{HOST}:{PORT}/mcp/")
    vlog.log(f"  flow GUI         : http://{HOST}:{PORT}/gui   (also /mcp/gui behind the prod proxy)")
    _all = tool_enablement.load()
    vlog.log(f"  tools ({len(_all)})       : {', '.join(_all) or '(none)'}   [config-driven; add/edit via GUI]")
    for _name, _cfg in _all.items():
        vlog.log(f"  • {_name}:")
        for _a in _cfg.analyzers:
            vlog.log(f"      - {_a.id:<6} {'[mandatory]' if _a.mandatory else '[optional] '} "
                     f"{'' if _a.enabled else '(disabled) '}→ {_a.api_url}")
        vlog.log(f"      ORB: {'ON' if _cfg.orb_enabled else 'OFF'}"
                 f"  ·  routing prompt: {'custom' if _cfg.routing_system_prompt.strip() else 'default'}")
    _dbs = db.status()
    vlog.log(f"  AI Controller    : {'ON → ' + ai_controller.GEN_URL if ai_controller.ENABLED else 'OFF (deterministic routing + discovery-built calls)'}"
             + (f"  (timeout {ai_controller.TIMEOUT:.0f}s)" if ai_controller.ENABLED else ""))
    vlog.log(f"  job history (DB) : "
             + (f"ON → {_dbs['url']}" if _dbs["connected"] else
                ("OFF — " + (_dbs["last_error"] or "no DATABASE_URL / disabled"))))
    vlog.log(f"  auth             : {'Bearer token (StaticTokenVerifier)' if auth else 'DISABLED ⚠️ (MCP_ALLOW_INSECURE)'}")
    vlog.log(f"  private fetch     : {'ALLOWED (dev)' if ALLOW_PRIVATE_FETCH else 'blocked (SSRF guard)'}")
    vlog.log(f"  outbound TLS     : {tlsconf.describe()}")
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
