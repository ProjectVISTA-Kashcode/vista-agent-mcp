"""AI CONTROLLER DECISION — which analyzers run, and how each one is called.

This step **always runs**. It is the orchestrator's one thinking step, and it now has two jobs:

1. **Selection** — pick which *optional* analyzers add value for this question (mandatory ones
   are always in the set). Driven by each analyzer's ``when_to_use`` from its live discovery.
2. **Invocation planning** — for every analyzer that will run, decide *how* to call it from the
   contract that analyzer just advertised: method, URL, content type, which param carries the
   file, which declared params get the question. See :mod:`orchestrator.plan`.

**Why it never skips.** Discovery is live: an analyzer can change its endpoint, rename a param,
or add a required one between two calls, and a brand-new analyzer can appear in the config at
any time. Skipping the controller when a tool happens to have no optional analyzers would mean
calling a *possibly-changed* API with a request shape assumed from the last time someone read
the docs. So the controller runs on every job — when there is nothing to select it runs in
``plan-only`` mode and still reads the fresh contracts. Discovery decides *what is possible*;
the controller decides *what to do about it*.

**Fail-safe, always.** If the AI gateway is off, unreachable or returns junk, the decision falls
back to the deterministic policy — include every optional analyzer (better to over-analyze than
to silently drop a relevant one) and build each request straight from its discovery document.
That fallback is exactly the behaviour VISTA-MCP had before the controller existed, so a gateway
outage is invisible to clients.

ORB is NOT decided here — it is a static config flag applied at the end of the pipeline.
"""
from __future__ import annotations

import time

import vlog

from . import ai_controller, plan as plan_mod
from .models import AnalyzerDiscovery, AnalyzerRef, CallPlan, Decision

# The routing system prompt has TWO parts:
#
#   1. A per-tool GUIDANCE section (``ToolConfig.routing_system_prompt``) — tailored to THIS
#      MCP tool and editable in config/the GUI, exactly like TOOL_ENABLEMENT. It tells the
#      controller what this tool is for and how to think about routing its analyzers. If a tool
#      doesn't set one, ``_DEFAULT_GUIDANCE`` is used.
#
#   2. A fixed OUTPUT CONTRACT (``_OUTPUT_CONTRACT``) — machinery, NOT editable, always appended.
#      It pins the JSON response shape the orchestrator parses, so a tool author can rewrite the
#      guidance freely without ever breaking the decision parsing.
#
# Composed: guidance + "\n\n" + output-contract. This is what "the system prompt changes per
# tool" means in practice — each tool supplies its own guidance section.
_DEFAULT_GUIDANCE = (
    "You are the AI Controller for a Fortinet VISTA MCP tool. You route a user's "
    "FortiGate/FortiOS support question to the right analyzers and you decide how each analyzer "
    "is invoked. You are given the user's question and the analyzers available right now, each "
    "with a description of when it should be used and the exact request contract it just "
    "advertised. Choose ONLY the optional analyzers whose purpose is directly relevant to "
    "answering THIS question. Do not include an analyzer just because it exists."
)

_OUTPUT_CONTRACT = """\
Respond with ONLY one JSON object and nothing else:

{
  "call": ["<optional analyzer id>", ...],
  "reason": "<one short sentence explaining the selection>",
  "invocations": [
    {"id": "<analyzer id>", "method": "<from its contract>", "url": "<from its contract>",
     "content_type": "<from its contract>", "file_param": "<the param that carries the file>",
     "fields": {"<declared param name>": "<value or placeholder>"},
     "note": "<optional, one short clause>"}
  ]
}

RULES — follow exactly:
- "call" lists ONLY the OPTIONAL analyzer ids you are adding. MANDATORY analyzers always run; \
never list them in "call". If no optional analyzer is relevant, use "call": [].
- "invocations" MUST contain one entry for EVERY analyzer that will run: every MANDATORY \
analyzer, plus every id you put in "call".
- Copy "method", "url" and "content_type" EXACTLY from that analyzer's "call:" line. Never \
invent or modify an endpoint.
- "file_param" is that analyzer's declared param whose location is "file" — the uploaded file is \
attached there. If it declares none, use "".
- "fields" may only contain params that analyzer DECLARES. Include every REQUIRED param. Omit \
optional params you do not need.
- Use the literal placeholder {{question}} where the user's question belongs, and {{filename}} \
for the file name. Do NOT paste the question text itself.
- If an analyzer declares no params at all, use "file_param": "file" and "fields": {}."""


def _system_prompt(routing_system_prompt: str) -> str:
    guidance = (routing_system_prompt or "").strip() or _DEFAULT_GUIDANCE
    return f"{guidance}\n\n{_OUTPUT_CONTRACT}"


def _build_prompt(system: str, question: str, filename: str, file_bytes: int,
                  mandatory: list[tuple[AnalyzerRef, AnalyzerDiscovery]],
                  optional: list[tuple[AnalyzerRef, AnalyzerDiscovery]]) -> str:
    lines = [system, ""]
    lines.append(f'INPUT FILE: "{filename}" ({file_bytes:,} bytes)')
    lines.append(f'USER QUESTION: "{question or "(no specific question — general analysis)"}"')
    lines.append("")
    if mandatory:
        lines.append("MANDATORY ANALYZERS (always run — plan an invocation for each):")
        lines += [plan_mod.describe_contract(r, d) for r, d in mandatory]
        lines.append("")
    if optional:
        lines.append("OPTIONAL ANALYZERS (choose the relevant ones):")
        lines += [plan_mod.describe_contract(r, d) for r, d in optional]
    else:
        lines.append("OPTIONAL ANALYZERS: (none configured — \"call\" must be [])")
    lines += ["", "Respond with the JSON object only."]
    return "\n".join(lines)


def _plans_for(refs: list[tuple[AnalyzerRef, AnalyzerDiscovery]], invocations: dict,
               question: str, filename: str) -> dict[str, CallPlan]:
    """One validated CallPlan per analyzer that will run (AI plan if usable, else built)."""
    out: dict[str, CallPlan] = {}
    for ref, disc in refs:
        raw = invocations.get(ref.id)
        if isinstance(raw, dict):
            p = plan_mod.from_ai(raw, ref, disc, question, filename)
        else:
            p = plan_mod.deterministic(ref, disc, question, filename)
            if invocations:
                p.notes.append("the AI Controller planned no invocation for this analyzer "
                               "→ built from its discovery")
        out[ref.id] = p
    return out


def _plan_source(plans: dict[str, CallPlan]) -> str:
    sources = {p.source for p in plans.values()}
    if not sources:
        return "deterministic"
    if len(sources) == 1:
        return sources.pop()
    return "mixed"


async def decide(
    question: str,
    mandatory: list[AnalyzerRef],
    optional: list[AnalyzerRef],
    discoveries: dict[str, AnalyzerDiscovery],
    routing_system_prompt: str = "",
    filename: str = "",
    file_bytes: int = 0,
) -> Decision:
    """Run the AI Controller for one job. Never raises; always returns a usable Decision."""
    mand_pairs = [(r, discoveries[r.id]) for r in mandatory if r.id in discoveries]
    opt_pairs = [(r, discoveries[r.id]) for r in optional if r.id in discoveries]
    mand_ids = [r.id for r, _ in mand_pairs]
    all_opt_ids = [r.id for r, _ in opt_pairs]
    system = _system_prompt(routing_system_prompt)
    mode = "select+plan" if opt_pairs else "plan-only"

    def _fallback(reason: str, *, used_ai: bool, raw: str = "",
                  elapsed_ms: int | None = None) -> Decision:
        """Deterministic policy: keep every optional analyzer, build every request from
        discovery. Identical to VISTA-MCP's pre-AI-Controller behaviour."""
        pairs = mand_pairs + opt_pairs
        plans = _plans_for(pairs, {}, question, filename)
        return Decision(
            selected_ids=mand_ids + all_opt_ids, used_ai=used_ai, reason=reason,
            system_prompt=system, ai_raw=raw, plans=plans, plan_source="deterministic",
            elapsed_ms=elapsed_ms, mode=mode,
        )

    if not ai_controller.ENABLED:
        return _fallback(
            "AI Controller disabled (AI_CONTROLLER_ENABLED=0) → deterministic policy: "
            f"running all analyzers ({', '.join(mand_ids + all_opt_ids) or 'none'})",
            used_ai=False,
        )

    prompt = _build_prompt(system, question, filename, file_bytes, mand_pairs, opt_pairs)
    vlog.log(f"  → [ai_controller] asking ({len(prompt):,}-char prompt · mode={mode} · "
             f"mandatory={mand_ids} optional={all_opt_ids})")
    vlog.log(f"  ai_controller prompt:\n{prompt}", vlog.DEBUG)
    t0 = time.time()
    answer = await ai_controller.ask(prompt)
    elapsed_ms = int((time.time() - t0) * 1000)

    if answer is None:
        return _fallback("AI Controller unavailable → fail-safe: running all analyzers with "
                         "requests built from discovery",
                         used_ai=False, elapsed_ms=elapsed_ms)

    parsed = ai_controller.extract_json(answer)
    if not isinstance(parsed, (dict, list)):
        return _fallback("AI Controller answer unparseable → fail-safe: running all analyzers "
                         "with requests built from discovery",
                         used_ai=True, raw=answer[:4000], elapsed_ms=elapsed_ms)

    # --- selection ---
    call: list[str] = []
    reason = ""
    invocations: dict[str, dict] = {}
    if isinstance(parsed, dict):
        call = [str(x) for x in (parsed.get("call") or [])]
        reason = str(parsed.get("reason") or "")
        for inv in (parsed.get("invocations") or []):
            if isinstance(inv, dict) and inv.get("id"):
                invocations[str(inv["id"])] = inv
    else:                                    # a bare list of ids is accepted too
        call = [str(x) for x in parsed]

    valid_opt = set(all_opt_ids)
    chosen = [cid for cid in call if cid in valid_opt]
    ignored = [cid for cid in call if cid not in valid_opt and cid not in mand_ids]

    # mandatory first, then the chosen optional in config order; de-duped
    selected = list(dict.fromkeys(mand_ids + [oid for oid in all_opt_ids if oid in chosen]))

    # --- invocation planning for exactly the analyzers that will run ---
    running_pairs = [(r, d) for r, d in mand_pairs + opt_pairs if r.id in selected]
    plans = _plans_for(running_pairs, invocations, question, filename)

    if not reason:
        reason = (f"AI Controller selected: {', '.join(chosen) or 'no optional analyzers'}"
                  if opt_pairs else
                  f"no optional analyzers configured — planned calls for {', '.join(mand_ids)}")
    if ignored:
        reason += f" (ignored unknown id(s): {', '.join(ignored)})"

    corrected = [p for p in plans.values() if p.source != "ai"]
    if corrected:
        vlog.log("  ⚠ [ai_controller] plan corrections: " + "; ".join(
            f"{p.analyzer_id}: {'; '.join(p.notes)}" for p in corrected if p.notes), vlog.WARNING)

    return Decision(
        selected_ids=selected, used_ai=True, reason=reason, system_prompt=system,
        ai_raw=answer[:4000], plans=plans, plan_source=_plan_source(plans),
        elapsed_ms=elapsed_ms, mode=mode,
    )
