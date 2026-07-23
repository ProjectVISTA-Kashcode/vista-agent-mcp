"""
LogV analyzer — backs the `Log_Analyzer_Visualizer` MCP tool.
=============================================================

Forwards a FortiOS/FortiGate **event** log (+ optional question) to the Log
Visualizer "AgentAssist" API and folds its structured JSON into ONE text block:

    filter summary + provenance  →  Log Visualizer link  →  AI analysis
      →  ORB troubleshooting suggestions  →  iframe URL

Endpoint chosen by whether a question was asked:
  * question given  → POST .../filter_analyze_visualyze_logs  (filter → analyze → visualize)
  * no question     → POST .../analyze_visualise_logs         (analyze → visualize)

AgentAssist itself classifies the log subtype (SD-WAN today) and returns a
`supported: false` payload for types it can't handle yet — we surface that verbatim.

ORB troubleshooting: LogV returns *only* the analysis; this MCP layer then asks the ORB
"ask" API for remediation steps directly relevant to that analysis and slots ORB's answer
into the report between the analysis and the visualization (see `_fetch_orb_suggestions`).
It is fail-open — any ORB error is logged and skipped, never blocking the report.

The internal reasoning-engine name is scrubbed from all forwarded text (`_scrub`) — including
the analysis we send out to ORB.
"""
from __future__ import annotations

import re
import time

import httpx

import vlog

from .base import Analyzer

# Max chars of the analysis section we place in the report (defense-in-depth: the backend
# already bounds it, but never let a runaway response blow up the agent's context).
MAX_ANALYSIS_CHARS = 40000

# Words to remove from forwarded text (don't leak the internal model/vendor name);
# matches DeepSeek / DeepSeek's / DeepSeeks (with or without curly apostrophe).
_SCRUB_RE = re.compile(r"\bdeepseek(?:['’]?s)?\b", re.IGNORECASE)


def _scrub(text: str) -> str:
    """Replace internal engine references with a neutral phrase."""
    if not text:
        return text or ""
    out = _SCRUB_RE.sub("the analysis engine", text)
    # tidy the common "the analysis engine made" → "Made" style double-ups
    out = re.sub(r"\bthe analysis engine made\b", "made", out, flags=re.IGNORECASE)
    return out


# --------------------------------------------------------------------------- #
# ORB troubleshooting — this MCP layer asks the ORB "ask" API for remediation steps
# directly relevant to the analysis LogV returned, and slots ORB's `answer` into the
# report between the analysis and the visualization. Config is injected by server.py.
# --------------------------------------------------------------------------- #
ORB_TROUBLESHOOT_PROMPT = (
    "How can I troubleshoot and only specify which is directly relevant to the issue"
)
# The user's own question is appended directly BELOW the troubleshoot prompt (when they asked
# one), so ORB targets its remediation at what the user actually wanted to know.
ORB_USER_QUESTION_LABEL = "The user's original question about these logs:"
# Report heading for the ORB block (matches the report's other `## `-style sections).
ORB_SECTION_HEADING = "## ORB Suggestions"
DEFAULT_ORB_ASK_URL = "http://172.17.96.58:9345/orb/api/ask"
DEFAULT_ORB_USERNAME = "logV_mcp_call"
DEFAULT_ORB_TIMEOUT = 180.0  # ORB is deep-research; it can take ~45-140s


TOOL_DESCRIPTION = (
    "Analyze and visualize a FortiOS / FortiGate **event** log file (e.g. FortiGate "
    "SD-WAN health-check / SLA logs; more FortiOS event types coming). The platform "
    "supplies the log automatically via the injected `source_url` field — you do NOT "
    "ask the user for the file and you do NOT fill `source_url`.\n\n"
    "What it does: ingests the log, auto-detects its type, and returns a single report "
    "combining (1) a filter summary of the logs most relevant to the user's question, "
    "(2) a link to open the logs in the Log Visualizer, (3) an AI analysis written for a "
    "Fortinet TAC engineer followed by a 'ORB Suggestions' section with troubleshooting "
    "steps directly relevant to the findings, and (4) a URL to an interactive dashboard you "
    "should embed as an iframe in your answer. Note: it runs full analysis + troubleshooting "
    "research, so it can take 1-2 minutes to respond.\n\n"
    "When to use: the user uploaded a FortiGate/FortiOS event log and wants it explained, "
    "triaged, searched, or visualized. Pass the user's ask in `question` (optional — omit "
    "for a general analysis + visualization). If the log type isn't supported yet, the tool "
    "says so explicitly."
)


class LogVAnalyzer(Analyzer):
    name = "Log_Analyzer_Visualizer"
    log_field = "source_url"

    def __init__(
        self,
        api_base: str,
        view_base: str | None = None,
        timeout: float = 300.0,
        *,
        orb_enabled: bool = True,
        orb_url: str = DEFAULT_ORB_ASK_URL,
        orb_username: str = DEFAULT_ORB_USERNAME,
        orb_timeout: float = DEFAULT_ORB_TIMEOUT,
    ):
        # e.g. http://logv-host:8802/logVisualizer/api/agent_assist
        self.api_base = api_base.rstrip("/")
        # SPA base used to build the shareable/iframe links (passed to AgentAssist as spa_base)
        self.view_base = (view_base or "").rstrip("/") or None
        self.timeout = timeout
        # ORB troubleshooting (fail-open; see _fetch_orb_suggestions)
        self.orb_enabled = orb_enabled
        self.orb_url = orb_url
        self.orb_username = orb_username
        self.orb_timeout = orb_timeout

    def description(self) -> str:
        return TOOL_DESCRIPTION

    async def _fetch_orb_suggestions(self, payload: dict, user_question: str = "") -> str | None:
        """Ask the ORB API for troubleshooting steps directly relevant to the analysis in
        `payload`, and return ORB's `answer` string only. The question sent to ORB is:

            <analysis>  +  ORB_TROUBLESHOOT_PROMPT  +  <the user's own question, if any>

        so the remediation is aimed at what the user actually asked. Fail-open and
        log-type-agnostic: returns None (never raises) when ORB is disabled, the payload has no
        real analysis (empty / AI-unavailable / 'no relevant logs'), or ORB errors/empties — so
        the report is never blocked by ORB. Everything is scrubbed before it leaves the MCP
        boundary, so the internal engine name is never sent to ORB."""
        if not self.orb_enabled:
            return None
        obj = payload.get("analysis", {})
        analysis_text = str((obj.get("analysis") if isinstance(obj, dict) else obj) or "").strip()
        if (
            not analysis_text
            or analysis_text == "Failed to query AI model."
            or analysis_text.lstrip().startswith("### No relevant logs")
        ):
            vlog.log("ORB: skipped (no real analysis to troubleshoot)")
            return None

        parts = [_scrub(analysis_text), ORB_TROUBLESHOOT_PROMPT]
        uq = _scrub((user_question or "").strip())
        if uq:  # the user's own question goes directly below the troubleshoot instruction
            parts.append(f"{ORB_USER_QUESTION_LABEL} {uq}")
        question = "\n\n".join(parts)
        vlog.log(
            f"ORB: requesting troubleshooting suggestions ({len(question):,}-char question, "
            f"user question={'yes: ' + uq[:80] if uq else 'none'}) → {self.orb_url}"
        )
        t0 = time.time()
        timeout = httpx.Timeout(connect=5.0, read=self.orb_timeout, write=30.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    self.orb_url,
                    json={"question": question, "username": self.orb_username},
                )
            resp.raise_for_status()
            answer = ((resp.json() or {}).get("answer") or "").strip()
        except Exception as e:  # noqa: BLE001  — never let ORB break the report
            vlog.log(f"ORB: suggestions unavailable ({type(e).__name__}: {e})", vlog.WARNING)
            return None
        if not answer:
            vlog.log(f"ORB: returned an empty answer in {time.time() - t0:.1f}s", vlog.WARNING)
            return None
        vlog.log(f"ORB: received {len(answer):,} chars of suggestions in {time.time() - t0:.1f}s")
        return _scrub(answer)

    async def analyze(self, *, log_bytes: bytes, filename: str, question: str) -> str:
        q = (question or "").strip()
        endpoint = "filter_analyze_visualyze_logs" if q else "analyze_visualise_logs"
        url = f"{self.api_base}/{endpoint}"

        data: dict[str, str] = {}
        if q:
            data["question"] = q
        if self.view_base:
            data["spa_base"] = self.view_base
        files = {"file": (filename or "logfile.log", log_bytes, "text/plain")}

        vlog.log(
            f"forward → LogV: POST {url}  (question={'yes' if q else 'no'}, "
            f"file={len(log_bytes):,}B, spa_base={self.view_base or '(default)'})"
        )
        t0 = time.time()
        timeout = httpx.Timeout(connect=5.0, read=self.timeout, write=30.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, data=data, files=files)
        except httpx.HTTPError as e:
            vlog.log(f"forward: LogV UNREACHABLE ({url}): {e}", vlog.ERROR)
            return f"⚠️ Could not reach the Log Analyzer backend ({url}): {e}"

        vlog.log(f"forward: LogV responded HTTP {resp.status_code} in {time.time()-t0:.2f}s")
        if resp.status_code >= 400:
            # AgentAssist returns JSON {"error": "..."} on 4xx/5xx
            detail = ""
            try:
                detail = resp.json().get("error", "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            vlog.log(f"forward: LogV error {resp.status_code}: {detail}", vlog.WARNING)
            return f"⚠️ The Log Analyzer rejected the request (HTTP {resp.status_code}): {detail}"

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            vlog.log("forward: LogV returned non-JSON body", vlog.WARNING)
            return f"⚠️ The Log Analyzer returned a non-JSON response: {resp.text[:200]}"

        _log_payload_summary(payload)
        # Ask ORB for troubleshooting steps relevant to this analysis AND to the user's own
        # question (fail-open — None on any issue), then fold them into the report between the
        # analysis and the visualization.
        orb_suggestions = await self._fetch_orb_suggestions(payload, user_question=q)
        report = _format_report(payload, orb_suggestions=orb_suggestions)
        vlog.log(f"report: built {len(report):,}-char text report from LogV JSON")
        return report


# --------------------------------------------------------------------------- #
# JSON → one concatenated text block
# --------------------------------------------------------------------------- #

def _log_payload_summary(data: dict) -> None:
    """Log the salient fields of the LogV response so the terminal shows what came back."""
    if data.get("supported") is False:
        vlog.log(f"LogV: log_type='{data.get('log_type')}' → NOT SUPPORTED (routing message)", vlog.WARNING)
        return
    viz = data.get("visualization", {}) or {}
    ana = data.get("analysis", {}) or {}
    filt = data.get("filter") or {}
    bits = [f"log_type={data.get('log_type')}", f"total_logs={data.get('total_logs')}"]
    if filt:
        bits.append(f"filter.matched={filt.get('matched_count')}/{filt.get('total_logs')}")
        bits.append(f"filter.queries={filt.get('query_count')}")
        if filt.get("fallback_used"):
            bits.append("filter.fallback=yes")
    if isinstance(ana, dict):
        atext = ana.get("analysis") or ""
        bits.append(f"analysis={len(atext):,}chars")
        if ana.get("context_trimmed"):
            bits.append(f"logs_sent_to_ai={ana.get('logs_sent_to_ai') or ana.get('logs_analyzed')}(trimmed)")
    bits.append(f"session_id={viz.get('session_id')}")
    bits.append(f"iframe={'yes' if viz.get('iframe_url') else 'no'}")
    vlog.log("LogV result: " + "  ".join(bits))
    vlog.log(f"LogV view_url: {viz.get('view_url')}")


def _format_report(data: dict, orb_suggestions: str | None = None) -> str:
    """Fold the AgentAssist JSON (filter/analyze/visualize or analyze/visualize, or a
    not-supported payload) into a single agent-readable text report. `orb_suggestions`, when
    present, is inserted as its own section between the AI analysis and the visualization."""
    # Not-supported routing (any non-SD-WAN log today)
    if data.get("supported") is False:
        lt = data.get("log_type", "unknown")
        supported = ", ".join(data.get("supported_log_types", [])) or "none listed"
        return (
            f"⚠️ The uploaded log was classified as **{lt}**, which the Log Analyzer & "
            f"Visualizer does not support yet. Supported log types: {supported}. "
            "Ask the user to upload a supported FortiOS event log."
        )

    log_type = data.get("log_type", "unknown")
    total = data.get("total_logs")
    parts: list[str] = [
        f"# 🔍 Log Analyzer & Visualizer — {log_type}"
        + (f"  ·  {total:,} logs" if isinstance(total, int) else "")
    ]

    # 1) FILTER (present only when a question drove a filter step)
    filt = data.get("filter")
    if isinstance(filt, dict):
        mc = filt.get("matched_count")
        tl = filt.get("total_logs", total)
        qc = filt.get("query_count", 0)
        parts.append(
            f"## Filter\n"
            f"filter: matched **{mc:,}/{tl:,}** · quer{'y' if qc == 1 else 'ies'} {qc}"
            if isinstance(mc, int) and isinstance(tl, int)
            else "## Filter\n(filter step ran)"
        )
        prov = _scrub(filt.get("provenance", "") or "")
        if prov:
            parts.append(prov)

    # 2) LINK to the Log Visualizer session (the exact session that was just created &
    #    analyzed — opening it shows the fully-parsed logs + dashboard for this run).
    viz = data.get("visualization", {}) or {}
    view_url = viz.get("view_url")
    if view_url:
        parts.append(
            "## View in Log Visualizer\n"
            "Open this link to view the analyzed logs directly in Log Visualizer "
            "(the exact session for this analysis):\n"
            f"{view_url}"
        )

    # 3) ANALYSIS (the AI report markdown)
    analysis_obj = data.get("analysis", {})
    analysis_text = (
        analysis_obj.get("analysis") if isinstance(analysis_obj, dict) else analysis_obj
    )
    if analysis_text:
        note = ""
        if isinstance(analysis_obj, dict) and analysis_obj.get("context_trimmed"):
            # no-question analyze path doesn't set logs_sent_to_ai → fall back to logs_analyzed
            sent = analysis_obj.get("logs_sent_to_ai") or analysis_obj.get("logs_analyzed")
            if sent is not None:
                note = f" _(analysis computed over {sent} logs that fit the context window)_\n\n"
        atext = _scrub(str(analysis_text))
        if len(atext) > MAX_ANALYSIS_CHARS:
            atext = (
                atext[:MAX_ANALYSIS_CHARS]
                + f"\n\n…(analysis truncated at {MAX_ANALYSIS_CHARS:,} chars — open the "
                "Log Visualizer link above for the full report)"
            )
        parts.append("## Analysis\n" + note + atext)

    # 3.5) ORB TROUBLESHOOTING SUGGESTIONS — added by this MCP layer, positioned AFTER the
    #      analysis and BEFORE the visualization. Already scrubbed by _fetch_orb_suggestions.
    if orb_suggestions:
        parts.append(f"{ORB_SECTION_HEADING}\n{orb_suggestions}")

    # 4) INTERACTIVE VISUALIZATION (iframe URL + render instruction to the agent)
    iframe = viz.get("iframe_url")
    if iframe:
        parts.append(
            "## Interactive Visualization\n"
            "Render the following URL as an `<iframe>` in your answer so the user can see "
            "the interactive dashboard (charts, filters, tables):\n"
            f"{iframe}"
        )

    return "\n\n".join(parts)
