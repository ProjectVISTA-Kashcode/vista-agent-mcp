"""DEEPSEEK DECISION — choose which analyzers to run for this question.

Rules (exactly as specified):

* **Mandatory** analyzers are ALWAYS in the call set.
* If there are **0 non-mandatory** analyzers, the decision is fully determined → **skip
  DeepSeek entirely** (ORB is orthogonal and never affects this).
* If there is **≥1 non-mandatory** analyzer, ask DeepSeek — given the user's question and each
  optional analyzer's ``when_to_use`` — which of them to add. Mandatory are always kept.

Fail-safe: if DeepSeek is unreachable or returns junk, fall back to including **all** optional
analyzers (better to over-analyze than to silently drop a relevant one), and say so.

ORB is NOT decided here — it is a static config flag applied at the end of the pipeline.
"""
from __future__ import annotations

from . import deepseek
from .models import AnalyzerDiscovery, AnalyzerRef, Decision

# The routing system prompt has TWO parts:
#
#   1. A per-tool GUIDANCE section (``ToolConfig.routing_system_prompt``) — tailored to THIS
#      MCP tool and editable in config/the GUI, exactly like TOOL_ENABLEMENT. It tells DeepSeek
#      what this tool is for and how to think about routing its analyzers. If a tool doesn't set
#      one, ``_DEFAULT_GUIDANCE`` is used.
#
#   2. A fixed OUTPUT CONTRACT (``_OUTPUT_CONTRACT``) — machinery, NOT editable, always appended.
#      It pins the JSON response shape the orchestrator parses, so a tool author can rewrite the
#      guidance freely without ever breaking the decision parsing.
#
# Composed: guidance + "\n\n" + output-contract. This is what "the system prompt changes per
# tool" means in practice — each tool supplies its own guidance section.
_DEFAULT_GUIDANCE = (
    "You route a user's FortiGate/FortiOS support question to the right analyzers. "
    "You are given the user's question and a list of OPTIONAL analyzers, each with a description "
    "of when it should be used. Choose ONLY the optional analyzers whose purpose is directly "
    "relevant to answering THIS question. Do not include an analyzer just because it exists."
)

_OUTPUT_CONTRACT = (
    'Respond with ONLY a JSON object and nothing else: '
    '{"call": ["<analyzer id>", ...], "reason": "<one short sentence>"}. '
    'List only the OPTIONAL analyzer ids you are adding. '
    'If none of the optional analyzers are relevant, return {"call": [], "reason": "..."}.'
)


def _system_prompt(routing_system_prompt: str) -> str:
    guidance = (routing_system_prompt or "").strip() or _DEFAULT_GUIDANCE
    return f"{guidance}\n\n{_OUTPUT_CONTRACT}"


def _build_prompt(
    system: str, question: str, optional: list[tuple[AnalyzerRef, AnalyzerDiscovery]]
) -> str:
    lines = [system, "", "OPTIONAL ANALYZERS:"]
    for ref, disc in optional:
        lines.append(f'- id="{ref.id}"  ({disc.analyzer.title}): {disc.analyzer.when_to_use}')
    lines += ["", f'USER QUESTION: "{question or "(no specific question — general analysis)"}"', ""]
    lines.append("Respond with the JSON object only.")
    return "\n".join(lines)


async def decide(
    question: str,
    mandatory: list[AnalyzerRef],
    optional: list[AnalyzerRef],
    discoveries: dict[str, AnalyzerDiscovery],
    routing_system_prompt: str = "",
) -> Decision:
    mand_ids = [r.id for r in mandatory]
    system = _system_prompt(routing_system_prompt)

    # optional analyzers that actually discovered OK (others were dropped upstream)
    opt_pairs = [(r, discoveries[r.id]) for r in optional if r.id in discoveries]

    if not opt_pairs:
        return Decision(
            selected_ids=mand_ids, used_deepseek=False, system_prompt=system,
            reason="no non-mandatory analyzers → DeepSeek decision skipped; "
                   f"calling mandatory only ({', '.join(mand_ids) or 'none'})",
        )

    prompt = _build_prompt(system, question, opt_pairs)
    answer = await deepseek.ask(prompt)

    if answer is None:
        chosen = [r.id for r, _ in opt_pairs]
        return Decision(
            selected_ids=mand_ids + chosen, used_deepseek=True, system_prompt=system,
            reason="DeepSeek unavailable → fail-safe: running all optional analyzers",
            deepseek_raw="",
        )

    parsed = deepseek.extract_json(answer)
    call = []
    reason = ""
    if isinstance(parsed, dict):
        call = [str(x) for x in (parsed.get("call") or [])]
        reason = str(parsed.get("reason") or "")
    elif isinstance(parsed, list):
        call = [str(x) for x in parsed]

    valid_opt = {r.id for r, _ in opt_pairs}
    chosen = [cid for cid in call if cid in valid_opt]

    if not isinstance(parsed, (dict, list)):
        chosen = [r.id for r, _ in opt_pairs]
        reason = "DeepSeek answer unparseable → fail-safe: running all optional analyzers"

    # de-dupe, keep mandatory first then chosen optional in config order
    selected = list(dict.fromkeys(mand_ids + [r.id for r, _ in opt_pairs if r.id in chosen]))
    return Decision(
        selected_ids=selected, used_deepseek=True, system_prompt=system,
        reason=reason or f"DeepSeek selected: {', '.join(chosen) or 'none'}",
        deepseek_raw=answer[:2000],
    )
