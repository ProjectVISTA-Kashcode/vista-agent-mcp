"""ORB — the built-in troubleshooting service (NOT an analyzer).

ORB is a static, config-driven step (``orb_enabled`` in TOOL_ENABLEMENT), never chosen by
DeepSeek. It runs **once at the end**: the concatenated analyzer report + a fixed troubleshoot
instruction + the user's original question are sent to the ORB "ask" API, and ORB's answer is
appended to the final report as a ``## ORB Suggestions`` section.

Uses the existing ORB ask API and format unchanged:

    POST <ORB_ASK_URL>   body: {"question": "<analysis>\\n\\n<instruction>\\n\\n<user q>",
                                 "username": "<ORB_USERNAME>"}   → {"answer": "..."}

**Fail-open**: any error (disabled, unreachable, empty, timeout) returns None and the report is
returned without an ORB section — ORB never blocks or breaks a response.
"""
from __future__ import annotations

import os
import time

import httpx

import tlsconf
import vlog

ORB_ASK_URL = os.getenv("ORB_ASK_URL", "https://vista.fortinet.com/orb/api/ask")
ORB_USERNAME = os.getenv("ORB_USERNAME", "logV_mcp_call")
ORB_TIMEOUT = float(os.getenv("ORB_TIMEOUT", "180"))

ORB_TROUBLESHOOT_PROMPT = (
    "How can I troubleshoot and only specify which is directly relevant to the issue"
)
ORB_USER_QUESTION_LABEL = "The user's original question about these logs:"
ORB_SECTION_HEADING = "## ORB Suggestions"


async def fetch(report_markdown: str, user_question: str = "") -> str | None:
    """Ask ORB for troubleshooting steps relevant to the concatenated report + question.

    Returns ORB's answer (already the section body), or None on any failure. Never raises.
    """
    analysis = (report_markdown or "").strip()
    if not analysis:
        vlog.log("  ⤼ [orb] skipped (empty report)", vlog.INFO)
        return None

    parts = [analysis, ORB_TROUBLESHOOT_PROMPT]
    uq = (user_question or "").strip()
    if uq:
        parts.append(f"{ORB_USER_QUESTION_LABEL} {uq}")
    question = "\n\n".join(parts)

    vlog.log(f"  → [orb] ask ({len(question):,}-char prompt, user_q={'yes' if uq else 'no'}) "
             f"→ {ORB_ASK_URL}")
    t0 = time.time()
    timeout = httpx.Timeout(connect=5.0, read=ORB_TIMEOUT, write=30.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(ORB_ASK_URL)) as client:
            resp = await client.post(
                ORB_ASK_URL, json={"question": question, "username": ORB_USERNAME}
            )
        resp.raise_for_status()
        answer = ((resp.json() or {}).get("answer") or "").strip()
    except Exception as e:  # noqa: BLE001 — never let ORB break the report
        vlog.log(f"  ✖ [orb] unavailable ({type(e).__name__}: {e})", vlog.WARNING)
        return None

    dt = int((time.time() - t0) * 1000)
    if not answer:
        vlog.log(f"  ✖ [orb] empty answer in {dt}ms", vlog.WARNING)
        return None
    vlog.log(f"  ✔ [orb] {len(answer):,} chars in {dt}ms")
    return answer
