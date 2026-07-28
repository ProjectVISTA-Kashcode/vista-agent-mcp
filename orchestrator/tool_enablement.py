"""TOOL_ENABLEMENT — the static per-tool config that drives the whole orchestrator.

**Adding an analyzer to a tool = one entry here. No code.** Each MCP tool
(``Log_Analyzer_Visualizer`` today) maps to a :class:`~orchestrator.models.ToolConfig`
listing the analyzers it may use (each ``mandatory`` or not) and whether ORB runs at the end.

The config is stored as JSON at ``config/tool_enablement.json`` so the GUI can edit it live.
This module loads it (with a safe default if the file is missing) and lets you reload/save it.

Per-analyzer fields (see :class:`~orchestrator.models.AnalyzerRef`):
    id           short id, e.g. "logv"
    api_url      analyzer base URL (the discovery advertises the query path under it)
    discover_url empty → derived as ``api_url + "/discover"``
    mandatory    true → ALWAYS called; false → DeepSeek decides from the user's question
    enabled      false → ignored entirely
"""
from __future__ import annotations

import json
import os
import threading

from .models import AnalyzerRef, ToolConfig

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
CONFIG_PATH = os.getenv(
    "TOOL_ENABLEMENT_PATH", os.path.join(_REPO, "config", "tool_enablement.json")
)

_LOCK = threading.Lock()
_CACHE: dict[str, ToolConfig] | None = None

# Listeners notified whenever the config changes (GUI save or reload). server.py registers one
# to (un)register MCP tools live, so adding a tool in the GUI exposes a new MCP tool with no
# restart and no code. Each listener is called with the fresh {tool_name: ToolConfig} mapping.
_LISTENERS: list = []


def on_change(fn) -> None:
    """Register a callback invoked with the new config dict after any save/reload."""
    _LISTENERS.append(fn)


def _notify(cfg: dict) -> None:
    for fn in list(_LISTENERS):
        try:
            fn(cfg)
        except Exception:  # noqa: BLE001 — a listener error must never break config saving
            pass

# Fallback config if the JSON is missing — matches today's behaviour (LogV mandatory + ORB).
_DEFAULT = {
    "tools": {
        "Log_Analyzer_Visualizer": {
            "description": "FortiOS/FortiGate log analysis + visualization.",
            "routing_system_prompt": (
                "You are the analyzer router for the FortiGate Log Analyzer & Visualizer tool. "
                "The log analysis is mandatory and already included. Decide only which OPTIONAL "
                "companion analyzers add value for the user's question; add none if the question "
                "is a general log/SLA/traffic question with no specialty need."
            ),
            "orb_enabled": True,
            "analyzers": [
                {
                    "id": "logv",
                    "title": "Log Analyzer & Visualizer",
                    "api_url": os.getenv(
                        "LOGV_API_BASE",
                        "http://127.0.0.1:8802/logVisualizer/api/agent_assist",
                    ),
                    "discover_url": "",
                    "mandatory": True,
                    "enabled": True,
                    "timeout": 300,
                }
            ],
        }
    }
}


def _parse(raw: dict) -> dict[str, ToolConfig]:
    out: dict[str, ToolConfig] = {}
    for tool_name, spec in (raw.get("tools") or {}).items():
        analyzers = [AnalyzerRef(**a) for a in (spec.get("analyzers") or [])]
        out[tool_name] = ToolConfig(
            tool_name=tool_name,
            description=spec.get("description", ""),
            routing_system_prompt=spec.get("routing_system_prompt", ""),
            orb_enabled=bool(spec.get("orb_enabled", True)),
            analyzers=analyzers,
        )
    return out


def load(force: bool = False) -> dict[str, ToolConfig]:
    """Return {tool_name: ToolConfig}. Cached; pass force=True to re-read the file."""
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not force:
            return _CACHE
        try:
            with open(CONFIG_PATH) as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = _DEFAULT
        except Exception:  # noqa: BLE001 — malformed file → default, don't crash the server
            raw = _DEFAULT
        _CACHE = _parse(raw)
        return _CACHE


def get(tool_name: str) -> ToolConfig | None:
    return load().get(tool_name)


def reload() -> dict[str, ToolConfig]:
    return load(force=True)


def raw_json() -> dict:
    """The raw JSON on disk (for the GUI editor)."""
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return _DEFAULT


def save_json(new_raw: dict) -> dict[str, ToolConfig]:
    """Persist a new config (validated by re-parsing) and refresh the cache. Used by the GUI."""
    parsed = _parse(new_raw)  # raises if invalid → caller keeps the old file
    with _LOCK:
        with open(CONFIG_PATH, "w") as fh:
            json.dump(new_raw, fh, indent=2)
        global _CACHE
        _CACHE = parsed
    _notify(parsed)  # let listeners (e.g. server.py) re-sync live MCP tools
    return parsed
