"""
vlog — shared, verbose terminal logging for VISTA-MCP.

Every tool call gets a short correlation id so the whole flow (request in → fetch →
forward to the analyzer backend → response → send back) is easy to follow in the
terminal, even with concurrent calls. Level via env MCP_LOG_LEVEL (INFO default;
DEBUG dumps prompts/report previews).
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import uuid
from urllib.parse import urlparse

logger = logging.getLogger("vista-mcp")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(
        logging.Formatter("%(asctime)s [VISTA-MCP] %(levelname)-5s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(_h)
    logger.setLevel(os.getenv("MCP_LOG_LEVEL", "INFO").upper())
    logger.propagate = False

_RID: "contextvars.ContextVar[str]" = contextvars.ContextVar("mcp_rid", default="--------")

# log-level shorthands so callers don't import `logging`
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR
DEBUG = logging.DEBUG


def new_rid() -> str:
    """Start a new correlation id for this request; returns it."""
    rid = uuid.uuid4().hex[:8]
    _RID.set(rid)
    return rid


def log(msg: str, level: int = INFO) -> None:
    logger.log(level, f"[{_RID.get()}] {msg}")


def short(text: object, n: int = 500) -> str:
    """One-line, length-capped rendering for log lines."""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def redact_url(url: str) -> str:
    """Show scheme+host+path but hide the (signed) query string in logs."""
    try:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}{p.path}"
        return base + ("?<signed-query-redacted>" if p.query else "")
    except Exception:  # noqa: BLE001
        return "<unparseable-url>"
