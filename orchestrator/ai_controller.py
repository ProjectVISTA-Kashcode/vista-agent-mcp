"""AI CONTROLLER — the gateway client the orchestrator thinks with.

This is the low-level transport only: send a prompt to the VISTA AI-MCP gateway, get text back,
pull JSON out of it. The *reasoning* (which analyzers to run and how to call each one) lives in
:mod:`orchestrator.decide`.

    POST https://vista.fortinet.com/ai-mcp/ds4/generate   (fallback: /ai-mcp/generate)
    body: {"prompt": "...", "tool": "IKE"}
    ok:   {"status":"completed", "result":{"status":"success", "answer":"..."}}

The AI Controller is **never** the analysis — analyzers do their own AI. It only makes the
routing + invocation decision (a short, structured question). Fail-safe by design: on any error
:func:`ask` returns ``None`` and the caller falls back to a deterministic policy built straight
from the discovery documents, so a gateway outage never blocks or changes a request.

Env (legacy ``DEEPSEEK_*`` names are still honoured as a fallback so existing deployments keep
working after the rename):

    AI_CONTROLLER_ENABLED   1 (default) — set 0 to force the deterministic path
    AI_CONTROLLER_GEN_URL   primary gateway URL      (legacy: DEEPSEEK_GEN_URL)
    AI_CONTROLLER_FALLBACK_URL   secondary gateway   (legacy: ANALYSIS_API_URL)
    AI_CONTROLLER_TIMEOUT   read timeout, seconds    (legacy: DEEPSEEK_TIMEOUT)
    AI_TOOL                 gateway `tool` selector (default IKE)
"""
from __future__ import annotations

import json
import os
import re

import httpx

import tlsconf
import vlog


def _env(name: str, legacy: str, default: str) -> str:
    """Prefer the new AI_CONTROLLER_* var, fall back to the legacy DEEPSEEK_* one."""
    return os.getenv(name) or os.getenv(legacy) or default


GEN_URL = _env("AI_CONTROLLER_GEN_URL", "DEEPSEEK_GEN_URL",
               "https://vista.fortinet.com/ai-mcp/ds4/generate")
FALLBACK_URL = _env("AI_CONTROLLER_FALLBACK_URL", "ANALYSIS_API_URL",
                    "https://vista.fortinet.com/ai-mcp/generate")
AI_TOOL = os.getenv("AI_TOOL", "IKE")
TIMEOUT = float(_env("AI_CONTROLLER_TIMEOUT", "DEEPSEEK_TIMEOUT", "60"))

# The one switch that turns the controller off entirely (deterministic orchestration).
ENABLED = os.getenv("AI_CONTROLLER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# Display name used in logs, the GUI and reports.
NAME = "AI Controller"


async def _post(url: str, prompt: str) -> str | None:
    timeout = httpx.Timeout(connect=5.0, read=TIMEOUT, write=15.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, verify=tlsconf.verify_for(url)) as client:
        resp = await client.post(
            url, json={"prompt": prompt, "tool": AI_TOOL},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json() or {}
    if data.get("status") == "completed" and data.get("result", {}).get("status") == "success":
        return (data["result"].get("answer") or "") or None
    vlog.log(f"ai-controller: unexpected response status={data.get('status')}", vlog.WARNING)
    return None


async def ask(prompt: str) -> str | None:
    """Send a prompt to the AI gateway (primary, then fallback). Returns the answer or None."""
    if not ENABLED:
        return None
    for label, url in (("primary", GEN_URL), ("fallback", FALLBACK_URL)):
        try:
            ans = await _post(url, prompt)
        except Exception as e:  # noqa: BLE001
            vlog.log(f"ai-controller: {label} gateway failed ({type(e).__name__}: {e})", vlog.WARNING)
            ans = None
        if ans is not None:
            if label != "primary":
                vlog.log("ai-controller: fell back to the secondary gateway endpoint", vlog.WARNING)
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
    # else the first {...} or [...] — greedy so nested objects survive
    m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            return None
    return None
