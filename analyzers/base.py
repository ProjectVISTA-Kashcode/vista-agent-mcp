"""
Analyzer abstraction — the pluggable unit behind each VISTA-MCP tool.
====================================================================

VISTA-MCP is a **proxy**: the partner agent calls an MCP tool, and we forward the
log (+ optional question) to the analyzer that backs that tool. Every analyzer has
a *different* backend API (Log Visualizer today; PCAP / FortiSIEM / … tomorrow), so
each one implements this small interface and owns:

  1. its **tool metadata** (`name`, `log_field`, `description`) — what the agent reads
     in `list_tools` to decide whether/how to call it, and
  2. its **`analyze()`** — how to forward the fetched log bytes + question to *its own*
     backend and reduce the result to a single text block the agent can reason over.

The MCP server (`server.py`) fetches the injected log URL, then hands the bytes to the
right analyzer. To add a new analyzer you subclass `Analyzer`, implement these two
things, and register one tiny tool function (see docs/ADDING_ANALYZERS.md).
"""
from __future__ import annotations

import abc


class Analyzer(abc.ABC):
    """One analyzer == one MCP tool. Subclasses forward to their own backend API."""

    #: MCP tool name the agent calls (keep it a clear verb-y noun, no spaces).
    name: str = "analyzer"

    #: The input field the PLATFORM injects the short-lived signed log URL into (the field
    #: name we give the partner at go-live). `server.register()` asserts this equals a real
    #: parameter of the tool function, so it can't drift from the tool's declared input.
    log_field: str = "source_url"

    @abc.abstractmethod
    def description(self) -> str:
        """The tool description shown in `list_tools`. The model decides *whether and
        how* to call the tool from this text alone — so be specific about what logs it
        handles, what it returns, and that the platform supplies the log."""

    @abc.abstractmethod
    async def analyze(self, *, log_bytes: bytes, filename: str, question: str) -> str:
        """Forward the (already-fetched) log to this analyzer's backend and return a
        single text block for the agent.

        Args:
            log_bytes: raw bytes of the fetched log file.
            filename:  best-effort filename (used to hint the backend at the format).
            question:  the user's natural-language question, or "" if none.

        Returns:
            One text string (the agent reads it as an observation). Keep it bounded;
            reduce, don't dump. On a handled error, return a short explanatory string
            rather than raising an internal stack trace.
        """
