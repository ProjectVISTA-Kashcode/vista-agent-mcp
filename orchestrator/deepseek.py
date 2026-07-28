"""DeepSeek gateway client — used by the orchestrator to DECIDE which analyzers to call.

Same VISTA AI-MCP gateway the Log Visualizer uses:

    POST https://vista.fortinet.com/ai-mcp/ds4/generate   (fallback: /ai-mcp/generate)
    body: {"prompt": "...", "tool": "IKE"}
    ok:   {"status":"completed", "result":{"status":"success", "answer":"..."}}

This is ONLY the tool-selection decision (a short structured question), never the analysis
itself — the analyzers do their own AI. Fail-safe: on any error the caller falls back to a
deterministic policy (call everything), so a gateway outage never blocks a request.
"""
from __future__ import annotations

import json
import os
import re

import httpx

import tlsconf
import vlog

DEEPSEEK_GEN_URL = os.getenv("DEEPSEEK_GEN_URL", "https://vista.fortinet.com/ai-mcp/ds4/generate")
ANALYSIS_API_URL = os.getenv("ANALYSIS_API_URL", "https://vista.fortinet.com/ai-mcp/generate")
AI_TOOL = os.getenv("AI_TOOL", "IKE")
DEEPSEEK_TIMEOUT = float(os.getenv("DEEPSEEK_TIMEOUT", "60"))


async def _post(url: str, prompt: str) -> str | None:
    timeout = httpx.Timeout(connect=5.0, read=DEEPSEEK_TIMEOUT, write=15.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
        resp = await client.post(
            url, json={"prompt": prompt, "tool": AI_TOOL},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json() or {}
    if data.get("status") == "completed" and data.get("result", {}).get("status") == "success":
        return (data["result"].get("answer") or "") or None
    vlog.log(f"deepseek: unexpected response status={data.get('status')}", vlog.WARNING)
    return None


async def ask(prompt: str) -> str | None:
    """Send a prompt to DeepSeek (falls back to AI-MCP). Returns the answer text or None."""
    for label, url in (("DeepSeek", DEEPSEEK_GEN_URL), ("AI-MCP", ANALYSIS_API_URL)):
        try:
            ans = await _post(url, prompt)
        except Exception as e:  # noqa: BLE001
            vlog.log(f"deepseek: {label} call failed ({type(e).__name__}: {e})", vlog.WARNING)
            ans = None
        if ans is not None:
            if label != "DeepSeek":
                vlog.log("deepseek: fell back to AI-MCP endpoint", vlog.WARNING)
            return ans
    return None


def extract_json(text: str):
    """Best-effort: pull the first JSON object/array out of a model answer."""
    if not text:
        return None
    # fenced ```json … ``` first
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    # else first {...} or [...]
    m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            return None
    return None
