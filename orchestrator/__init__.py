"""VISTA-MCP orchestrator — modular, dynamic, config-driven analyzer routing.

Public entry point:

    from orchestrator import pipeline
    report = await pipeline.run(tool_name, file_bytes, filename, question)

See ``docs/Architecture.md`` for the full flow and ``docs/how_to_add_analyzer.md`` to add one.
"""
from __future__ import annotations

from . import jobs, pipeline, tool_enablement

__all__ = ["pipeline", "jobs", "tool_enablement"]
