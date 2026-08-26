"""AI CONTROLLER DECISION — which analyzers run, and how each one is called.

This step **always runs**. It is the orchestrator's one thinking step, and it has two jobs:

1. **Selection** — pick which *optional* analyzers add value for this question (mandatory ones
   are always in the set). Driven by each analyzer's ``when_to_use`` from its live discovery.
2. **Invocation planning** — for every analyzer that will run, decide *how* to call it from the
   contract that analyzer just advertised: method, URL, content type, which param carries the
   file, which declared params get which values. See :mod:`orchestrator.plan`.

The decision is made by a **typed Pydantic AI agent**
(:func:`orchestrator.ai_controller.route_and_plan`), so the answer arrives as a validated
:class:`~orchestrator.ai_controller.RoutingDecision` — there is no hand-parsing of model text
here, and a malformed answer is re-prompted by Pydantic AI before it ever reaches this module.

**Why it never skips.** Discovery is live: an analyzer can change its endpoint, rename a param,
or add a required one between two calls, and a brand-new analyzer can appear in the config at
any time. Skipping the controller when a tool happens to have no optional analyzers would mean
calling a *possibly-changed* API with a request shape assumed from the last time someone read
the docs. So the controller runs on every job — when there is nothing to select it runs in
``plan-only`` mode and still reads the fresh contracts. Discovery decides *what is possible*;
the controller decides *what to do about it*.

**Fail-safe, always.** If the AI gateway is off, unreachable or answers unusably, the decision
falls back to the deterministic policy — include every optional analyzer (better to over-analyze
than to silently drop a relevant one) and build each request straight from its discovery
document. That fallback is exactly the behaviour VISTA-MCP had before the controller existed, so
a gateway outage is invisible to clients.

**The prompt has two parts.** A per-tool GUIDANCE section (``ToolConfig.routing_system_prompt``,
editable in the console) that says what this tool is for, and the fixed OUTPUT CONTRACT that
lives in :mod:`orchestrator.ai_controller` as the agent's instructions. A tool author can rewrite
the guidance freely without ever being able to break the decision parsing — the shape is enforced
by the Pydantic schema, not by prose.

ORB is NOT decided here — it is a static config flag applied at the end of the pipeline.
"""
from __future__ import annotations

import vlog

from . import ai_controller, plan as plan_mod
from .models import AnalyzerDiscovery, AnalyzerRef, CallPlan, Decision

_DEFAULT_GUIDANCE = (
    "You are the AI Controller for a Fortinet VISTA MCP tool. You route a user's "
    "FortiGate/FortiOS support question to the right analyzers and you decide how each analyzer "
    "is invoked. You are given the user's question and the analyzers available right now, each "
    "with a description of when it should be used and the exact request contract it just "
    "advertised. Choose ONLY the optional analyzers whose purpose is directly relevant to "
    "answering THIS question. Do not include an analyzer just because it exists."
)


def _system_prompt(routing_system_prompt: str) -> str:
    """The full prompt text recorded on the job (per-tool guidance + the fixed agent contract)."""
    guidance = (routing_system_prompt or "").strip() or _DEFAULT_GUIDANCE
    return f"{guidance}\n\n--- fixed agent contract ---\n{ai_controller._ROUTING_CONTRACT}"


def _build_prompt(guidance: str, question: str, filename: str, file_bytes: int,
                  mandatory: list[tuple[AnalyzerRef, AnalyzerDiscovery]],
                  optional: list[tuple[AnalyzerRef, AnalyzerDiscovery]]) -> str:
    lines = [guidance, ""]
    if filename or file_bytes:
        lines.append(f'INPUT FILE: "{filename}" ({file_bytes:,} bytes)')
    else:
        lines.append("INPUT FILE: (none — this call carries no file; source values from the "
                     "question)")
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
        lines.append('OPTIONAL ANALYZERS: (none configured — "call" must be [])')
    return "\n".join(lines)


def _invocation_dicts(decision: ai_controller.RoutingDecision) -> dict[str, dict]:
    """Flatten the agent's typed invocations into the plain dicts :mod:`plan` validates.

    ``fields`` arrives as a list of name/value pairs rather than a free-form object — models get
    free-form JSON objects wrong far more often than they get a list of pairs wrong, and the real
    types are recovered from the discovery in :meth:`AnalyzerParam.coerce`.
    """
    out: dict[str, dict] = {}
    for inv in decision.invocations:
        if not inv.id:
            continue
        out[inv.id] = {
            "method": inv.method, "url": inv.url, "content_type": inv.content_type,
            "file_param": inv.file_param, "note": inv.note,
            "fields": {fv.name: fv.value for fv in inv.fields if fv.name},
        }
    return out


def _plans_for(refs: list[tuple[AnalyzerRef, AnalyzerDiscovery]], invocations: dict,
               question: str, filename: str, file_text: str) -> dict[str, CallPlan]:
    """One validated CallPlan per analyzer that will run (AI plan if usable, else built)."""
    out: dict[str, CallPlan] = {}
    for ref, disc in refs:
        raw = invocations.get(ref.id)
        if isinstance(raw, dict):
            p = plan_mod.from_ai(raw, ref, disc, question, filename, file_text)
        else:
            p = plan_mod.deterministic(ref, disc, question, filename, file_text)
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
    file_text: str = "",
) -> Decision:
    """Run the AI Controller for one job. Never raises; always returns a usable Decision."""
    mand_pairs = [(r, discoveries[r.id]) for r in mandatory if r.id in discoveries]
    opt_pairs = [(r, discoveries[r.id]) for r in optional if r.id in discoveries]
    mand_ids = [r.id for r, _ in mand_pairs]
    all_opt_ids = [r.id for r, _ in opt_pairs]
    guidance = (routing_system_prompt or "").strip() or _DEFAULT_GUIDANCE
    system = _system_prompt(routing_system_prompt)
    mode = "select+plan" if opt_pairs else "plan-only"

    def _fallback(reason: str, *, used_ai: bool, raw: str = "",
                  elapsed_ms: int | None = None) -> Decision:
        """Deterministic policy: keep every optional analyzer, build every request from
        discovery. Identical to VISTA-MCP's pre-AI-Controller behaviour."""
        pairs = mand_pairs + opt_pairs
        plans = _plans_for(pairs, {}, question, filename, file_text)
        return Decision(
            selected_ids=mand_ids + all_opt_ids, used_ai=used_ai, reason=reason,
            system_prompt=system, ai_raw=raw, plans=plans, plan_source="deterministic",
            elapsed_ms=elapsed_ms, mode=mode,
        )

    if not ai_controller.ENABLED:
        return _fallback(
            f"AI Controller off ({ai_controller.ai_model.DISABLED_REASON}) → deterministic "
            f"policy: running all analyzers ({', '.join(mand_ids + all_opt_ids) or 'none'})",
            used_ai=False,
        )

    prompt = _build_prompt(guidance, question, filename, file_bytes, mand_pairs, opt_pairs)
    vlog.log(f"  → [ai_controller] asking ({len(prompt):,}-char prompt · mode={mode} · "
             f"mandatory={mand_ids} optional={all_opt_ids})")
    vlog.log(f"  ai_controller prompt:\n{prompt}", vlog.DEBUG)

    answer, elapsed_ms, note = await ai_controller.route_and_plan(prompt)

    if answer is None:
        return _fallback(f"AI Controller unavailable ({note}) → fail-safe: running all analyzers "
                         f"with requests built from discovery",
                         used_ai=False, elapsed_ms=elapsed_ms)

    # --- selection ---
    call = [str(x) for x in (answer.call or [])]
    reason = (answer.reason or "").strip()
    invocations = _invocation_dicts(answer)

    valid_opt = set(all_opt_ids)
    chosen = [cid for cid in call if cid in valid_opt]
    # A mandatory id listed in "call" is a harmless contract slip (it runs anyway), not an error.
    ignored = [cid for cid in call if cid not in valid_opt and cid not in mand_ids]

    # mandatory first, then the chosen optional in config order; de-duped
    selected = list(dict.fromkeys(mand_ids + [oid for oid in all_opt_ids if oid in chosen]))

    # --- invocation planning for exactly the analyzers that will run ---
    running_pairs = [(r, d) for r, d in mand_pairs + opt_pairs if r.id in selected]
    plans = _plans_for(running_pairs, invocations, question, filename, file_text)

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
        ai_raw=f"[{note}]\n{answer.model_dump_json(indent=1)}"[:4000],
        plans=plans, plan_source=_plan_source(plans), elapsed_ms=elapsed_ms, mode=mode,
    )
