"""AI MODEL — the Pydantic AI foundation every thinking step in VISTA-MCP runs on.

One place builds models, so every AI step in the orchestrator shares the same provider, the same
TLS policy, the same timeout, and the same fail-safe contract.

**The provider** is the Fortinet AgentAssist gateway, which speaks the OpenAI chat-completions
API (vLLM behind it) and supports both ``response_format: json_schema`` and native tool calling —
so Pydantic AI's typed :class:`~pydantic_ai.Agent` works against it directly and every answer the
orchestrator consumes is a **validated Pydantic model**, never hand-parsed text.

**Three tiers**, picked per task rather than one model for everything:

    fast   ``model-fast``  routing + invocation planning. Sub-second; this runs on every job.
    pro    ``model-pro``   authoring work — drafting a tool config from a discovery document.
    long   ``model-long``  large inputs — a big catalog, a long config, a report to normalise.

**Fail-safe contract.** :func:`run` NEVER raises and NEVER blocks a tool call: on a disabled
controller, a missing key, a gateway error, a timeout, or a response that doesn't validate, it
returns ``None`` and the caller falls back to its deterministic path. That is the same guarantee
the pre-Pydantic-AI controller gave, kept deliberately: an AI outage must be invisible to clients.

Env::

    AI_CONTROLLER_ENABLED   1 (default) — 0 forces every caller onto its deterministic path
    AGENTASSIST_BASE_URL    https://agentassist.corp.fortinet.com/v1
    AGENTASSIST_API_KEY     bearer key for the gateway (REQUIRED for any AI step to run)
    AI_MODEL_FAST           model-fast   (routing/planning)
    AI_MODEL_PRO            model-pro    (config authoring)
    AI_MODEL_LONG           model-long   (large documents)
    AI_TIMEOUT              per-request timeout, seconds (default 60)
    AI_TEMPERATURE          default 0 — these are decisions, not prose
    AI_MAX_TOKENS           default 4096
"""
from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

import tlsconf
import vlog

T = TypeVar("T", bound=BaseModel)


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


BASE_URL = os.getenv("AGENTASSIST_BASE_URL", "https://agentassist.corp.fortinet.com/v1").rstrip("/")
API_KEY = os.getenv("AGENTASSIST_API_KEY", "").strip()

MODEL_FAST = os.getenv("AI_MODEL_FAST", "model-fast")
MODEL_PRO = os.getenv("AI_MODEL_PRO", "model-pro")
MODEL_LONG = os.getenv("AI_MODEL_LONG", "model-long")
TIERS = {"fast": MODEL_FAST, "pro": MODEL_PRO, "long": MODEL_LONG}

TIMEOUT = float(os.getenv("AI_TIMEOUT", "60"))
TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0"))
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "4096"))

# The one switch that turns every AI step off (fully deterministic orchestration).
ENABLED = _flag("AI_CONTROLLER_ENABLED") and bool(API_KEY)

#: Why the controller is off, for the startup banner and the GUI status panel.
DISABLED_REASON = (
    "" if ENABLED
    else ("AI_CONTROLLER_ENABLED=0" if not _flag("AI_CONTROLLER_ENABLED")
          else "AGENTASSIST_API_KEY is not set")
)

NAME = "AI Controller"


def model_for(tier: str) -> str:
    """Resolve a tier name ('fast'|'pro'|'long') to a configured model id."""
    return TIERS.get((tier or "fast").lower(), MODEL_FAST)


@lru_cache(maxsize=1)
def _provider() -> OpenAIProvider:
    """The shared provider.

    Built on an http client that goes through :mod:`tlsconf`, so the gateway obeys the SAME
    outbound TLS policy as every other call this server makes (``MCP_FETCH_CA_BUNDLE`` /
    ``MCP_FETCH_INSECURE_TLS_HOSTS``). An internal-CA gateway therefore needs no special case.

    Pydantic AI deprecated ``httpx.AsyncClient`` for OpenAI-compatible providers in favour of
    ``httpx2``; prefer that when it is installed and fall back otherwise, so this works on either.
    """
    try:
        import httpx2

        client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(connect=5.0, read=TIMEOUT, write=30.0, pool=5.0),
            verify=tlsconf.verify_for(BASE_URL),
        )
    except Exception:  # noqa: BLE001 — httpx2 absent or rejects our args
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=TIMEOUT, write=30.0, pool=5.0),
            verify=tlsconf.verify_for(BASE_URL),
        )
    return OpenAIProvider(base_url=BASE_URL, api_key=API_KEY or "unset", http_client=client)


@lru_cache(maxsize=8)
def _model(tier: str) -> OpenAIChatModel:
    return OpenAIChatModel(model_for(tier), provider=_provider())


def _settings() -> ModelSettings:
    return ModelSettings(temperature=TEMPERATURE, max_tokens=MAX_TOKENS, timeout=TIMEOUT)


def agent(output_type: type[T], instructions: str, tier: str = "fast",
          name: str = "vista") -> Agent:
    """Build a typed Pydantic AI agent whose answer is validated into ``output_type``."""
    return Agent(
        _model(tier),
        output_type=output_type,
        instructions=instructions,
        model_settings=_settings(),
        retries=2,                    # Pydantic AI re-prompts the model on a validation failure
        name=name,
    )


async def run(output_type: type[T], instructions: str, prompt: str, *, tier: str = "fast",
              label: str = "ai") -> tuple[T | None, int, str]:
    """Run one typed AI step. Returns ``(output | None, elapsed_ms, raw_note)``. Never raises.

    ``raw_note`` is a short audit string (the model used, token counts, or the failure) kept on
    the job so the console can show what the controller actually did.
    """
    if not ENABLED:
        return None, 0, f"disabled ({DISABLED_REASON})"

    t0 = time.time()
    try:
        result = await agent(output_type, instructions, tier=tier, name=label).run(prompt)
    except Exception as e:  # noqa: BLE001 — an AI failure must never surface to a client
        ms = int((time.time() - t0) * 1000)
        vlog.log(f"  ✖ [{label}] {model_for(tier)} failed in {ms}ms "
                 f"({type(e).__name__}: {vlog.short(e, 200)})", vlog.WARNING)
        return None, ms, f"{type(e).__name__}: {vlog.short(e, 300)}"

    ms = int((time.time() - t0) * 1000)
    usage = getattr(result, "usage", None)
    note = f"{model_for(tier)} ({tier})"
    if usage is not None:
        note += (f" · in={getattr(usage, 'input_tokens', '?')} "
                 f"out={getattr(usage, 'output_tokens', '?')} tokens")
    vlog.log(f"  ✔ [{label}] {note} in {ms}ms")
    return result.output, ms, note


async def health() -> dict:
    """Probe the gateway with a trivial typed call — powers the console's AI status panel."""
    if not ENABLED:
        return {"ok": False, "enabled": False, "reason": DISABLED_REASON,
                "base_url": BASE_URL, "models": TIERS}

    class _Ping(BaseModel):
        ok: bool

    out, ms, note = await run(_Ping, "Answer with ok=true.", "ping", tier="fast", label="ai:health")
    return {
        "ok": out is not None, "enabled": True, "base_url": BASE_URL, "models": TIERS,
        "elapsed_ms": ms, "note": note,
        "reason": "" if out is not None else note,
    }


def describe() -> str:
    """One-line summary for the startup banner."""
    if not ENABLED:
        return f"OFF — {DISABLED_REASON} (deterministic routing + discovery-built calls)"
    return (f"ON → {BASE_URL}  [Pydantic AI · fast={MODEL_FAST} pro={MODEL_PRO} "
            f"long={MODEL_LONG}]  (timeout {TIMEOUT:.0f}s)")
