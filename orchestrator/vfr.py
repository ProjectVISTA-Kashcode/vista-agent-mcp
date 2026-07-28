"""VFR — Vetting & Flow Routing (placeholder).

In the target architecture the file first passes through **VFR**, which will (in a future
implementation by Lukas) inspect the file and confirm/route it to the correct analyzer(s)
before TOOL_ENABLEMENT and discovery run.

**For now VFR is an intentional no-op**: it takes the file bytes, returns them unchanged, and
reports ``"Correct analyzer match"``. It exists as a real seam in the pipeline (logged and shown
in the flow GUI) so the routing logic can be dropped in later with zero pipeline changes.

See ``docs/Architecture.md`` → "Next improvements: VFR" for the intended logic.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VFRResult:
    file_bytes: bytes
    filename: str
    matched: bool
    message: str


def run(file_bytes: bytes, filename: str) -> VFRResult:
    """No-op passthrough. Returns the same file and a fixed 'match' verdict.

    FUTURE (Lukas): sniff the file, decide which analyzer(s) it belongs to, and either
    short-circuit an obviously-wrong upload or annotate the routing hint used downstream.
    """
    return VFRResult(
        file_bytes=file_bytes,
        filename=filename,
        matched=True,
        message="Correct analyzer match",
    )
