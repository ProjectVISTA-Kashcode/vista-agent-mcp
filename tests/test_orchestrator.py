#!/usr/bin/env python3
"""End-to-end checks for the VISTA-MCP orchestrator — run them, don't trust the docs.

    .venv/bin/python tests/test_orchestrator.py            # everything
    .venv/bin/python tests/test_orchestrator.py --offline  # skip anything needing the network
    .venv/bin/python tests/test_orchestrator.py -k catalog # only tests whose name matches

What each group protects:

  regression  The LogV path is the one thing that must not move. These assert the deterministic
              plan for the classic ``file`` + ``question`` contract is byte-for-byte what it has
              always been, and that a tool config written before any of this existed still
              parses with the same behaviour.
  catalog     A ``/discover`` that advertises several analyzers (ORB) is parsed, and a config
              entry can select one out of it or expand into all of them.
  typing      A JSON contract's declared types survive planning — the ``boolean`` default ORB
              advertises used to fail validation outright and silently drop the analyzer.
  render      An analyzer result carrying no ``report_markdown`` still produces a section.
  live        Real calls: the AgentAssist gateway, ORB discovery, and full pipeline runs for
              each configured tool.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_REPO, ".env"))
except Exception:  # noqa: BLE001
    pass

from orchestrator import ai_model, discovery, plan as plan_mod, report, tool_enablement  # noqa: E402
from orchestrator.models import AnalyzerDiscovery, AnalyzerRef  # noqa: E402

PASS, FAIL, SKIP = "\033[32m✔\033[0m", "\033[31m✖\033[0m", "\033[33m⤼\033[0m"
_RESULTS: list[tuple[str, str, str]] = []
_TESTS: list[tuple[str, str, object]] = []


def test(group: str):
    def deco(fn):
        _TESTS.append((group, fn.__name__, fn))
        return fn
    return deco


def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}\n     expected: {expected!r}\n     actual:   {actual!r}")


def ok(cond, what: str) -> None:
    if not cond:
        raise AssertionError(what)


# =========================================================================== #
# fixtures — the exact documents the real services return
# =========================================================================== #
LOGV_DISCOVERY = {
    "schema_version": "1.0", "surface": "analyzer",
    "base_url": "https://vista.fortinet.com/logVisualizer/api/agent_assist",
    "analyzer": {"id": "logv", "title": "Log Analyzer & Visualizer",
                 "summary": "FortiGate log analysis + visualization.",
                 "when_to_use": "Use for FortiOS/FortiGate log analysis.",
                 "input_types": ["log"], "supported_log_types": ["sdwan"]},
    "query": {"method": "POST",
              "path": "https://vista.fortinet.com/logVisualizer/api/agent_assist/run",
              "content_type": "multipart/form-data",
              "params": [
                  {"name": "file", "required": True, "type": "file", "location": "file",
                   "description": "The log file."},
                  {"name": "question", "required": False, "type": "string", "location": "form",
                   "description": "The user's question."}]},
}

# ORB — a CATALOG document, and note config-validate's boolean `default`.
ORB_CATALOG = {
    "schema_version": "1.0",
    "analyzers": [
        {"schema_version": "1.0", "surface": "analyzer",
         "base_url": "https://vista.fortinet.com/orb/config-extract",
         "analyzer": {"id": "config-extract", "title": "FortiGate Config Extraction",
                      "summary": "Index a config and return relevant sections.",
                      "when_to_use": "Use when the user has a .conf and asks which parts matter.",
                      "input_types": ["conf"], "supported_log_types": []},
         "query": {"method": "POST",
                   "path": "https://vista.fortinet.com/orb/config-extract/run",
                   "content_type": "multipart/form-data",
                   "params": [
                       {"name": "file", "required": True, "type": "file", "location": "file",
                        "description": "The FortiGate .conf to analyze."},
                       {"name": "question", "required": True, "type": "string", "location": "form",
                        "description": "Scopes which sections to return."}]}},
        {"schema_version": "1.0", "surface": "analyzer",
         "base_url": "https://vista.fortinet.com/orb/config-validate",
         "analyzer": {"id": "config-validate", "title": "FortiGate Config Validation",
                      "summary": "Validate a snippet against the CLI Reference.",
                      "when_to_use": "Use when the user asks if commands are valid on a version.",
                      "input_types": ["text"], "supported_log_types": []},
         "query": {"method": "POST",
                   "path": "https://vista.fortinet.com/orb/config-validate/run",
                   "content_type": "application/json",
                   "params": [
                       {"name": "config_snippet", "required": True, "type": "string",
                        "location": "body", "description": "Config text to validate."},
                       {"name": "version", "required": True, "type": "string",
                        "location": "body", "description": "FortiOS version, e.g. '7.4.11'."},
                       {"name": "check_lines", "required": False, "type": "boolean",
                        "location": "body", "default": True,
                        "description": "Also validate each set/unset option."}]}},
    ],
}

ORB_VALIDATE_RESULT = {
    "analyzer": "config-validate", "error": None, "ok": True, "schema_version": "1.0",
    "meta": {"ran": True, "secs": 0.14, "version": "7.4.11", "version_defaulted": False},
    "reasoning": "Validated against FortiOS 7.4.11. 1/1 command paths and 1/1 set-lines exist.",
    "result": {"counts": {"lines_total": 1, "lines_validated": 1, "total": 1, "validated": 1},
               "flagged": [], "flagged_lines": [], "note": "", "ok": True,
               "structural": {"balanced": True, "edit_balanced": True}},
}

SAMPLE_CONF = "config router bgp\n    set as 65001\n    set router-id 1.1.1.1\nend\n"


# =========================================================================== #
# regression — the LogV path must not move
# =========================================================================== #
@test("regression")
async def logv_deterministic_plan_is_unchanged():
    """The classic file+question contract must plan byte-for-byte what it always did."""
    disc = AnalyzerDiscovery.model_validate(LOGV_DISCOVERY)
    ref = AnalyzerRef(id="logv", title="Log Analyzer & Visualizer",
                      api_url="https://vista.fortinet.com/logVisualizer/api/agent_assist",
                      mandatory=True)
    p = plan_mod.deterministic(ref, disc, "SLA failures on Austin?", "sdwan.log")
    eq(p.method, "POST", "method")
    eq(p.url, "https://vista.fortinet.com/logVisualizer/api/agent_assist/run", "url")
    eq(p.content_type, "multipart/form-data", "content type")
    eq(p.file_param, "file", "file param")
    eq(p.fields, {"question": "SLA failures on Austin?"}, "fields")
    eq(p.notes, [], "no correction notes")
    eq(p.source, "deterministic", "source")


@test("regression")
async def logv_plan_ignores_file_text():
    """Passing file_text must not leak into a multipart contract that declares a file param."""
    disc = AnalyzerDiscovery.model_validate(LOGV_DISCOVERY)
    ref = AnalyzerRef(id="logv", api_url="x", mandatory=True)
    p = plan_mod.deterministic(ref, disc, "q", "a.log", file_text="SECRET CONFIG TEXT")
    eq(p.fields, {"question": "q"}, "file text must not appear in a multipart plan")


@test("regression")
async def config_without_new_keys_still_parses():
    """A tool config written before catalogs / text-input tools keeps its old behaviour."""
    old = {"tools": {"T": {"description": "d", "orb_enabled": True,
                           "analyzers": [{"id": "a", "api_url": "http://x/base"}]}}}
    parsed = tool_enablement._parse(old)
    eq(parsed["T"].require_source_url, True, "require_source_url defaults to True")
    eq(parsed["T"].analyzers[0].catalog_select, "", "catalog_select defaults to empty")
    eq(parsed["T"].analyzers[0].resolved_discover_url(), "http://x/base/discover", "discover url")


@test("regression")
async def live_tools_are_distinguishable():
    """Every configured tool needs a distinct description — it is the whole routing signal."""
    cfg = tool_enablement.load(force=True)
    seen: dict[str, str] = {}
    for name, tc in cfg.items():
        d = " ".join((tc.description or "").split()).lower()
        ok(d, f"tool '{name}' has no description")
        ok(d not in seen, f"tools '{seen.get(d)}' and '{name}' have IDENTICAL descriptions")
        seen[d] = name


# =========================================================================== #
# catalog — a /discover advertising several analyzers
# =========================================================================== #
@test("catalog")
async def catalog_document_is_recognised():
    ok(discovery.is_catalog(ORB_CATALOG), "ORB document must be detected as a catalog")
    ok(not discovery.is_catalog(LOGV_DISCOVERY), "LogV document must NOT be a catalog")
    cat = discovery.parse(ORB_CATALOG)
    eq(len(cat.analyzers), 2, "catalog analyzer count")
    eq([a.analyzer.id for a in cat.analyzers], ["config-extract", "config-validate"], "ids")


@test("catalog")
async def catalog_select_picks_one():
    cat = discovery.parse(ORB_CATALOG)
    ref = AnalyzerRef(id="config-validate", api_url="https://vista.fortinet.com/orb",
                      catalog_select="config-validate", mandatory=True)
    pairs = discovery._select(ref, cat)
    eq(len(pairs), 1, "one analyzer selected")
    eq(pairs[0][1].analyzer.id, "config-validate", "the right one")
    eq(pairs[0][0].id, "config-validate", "ref id preserved")


@test("catalog")
async def catalog_expands_when_no_selector():
    cat = discovery.parse(ORB_CATALOG)
    ref = AnalyzerRef(id="orb", api_url="https://vista.fortinet.com/orb", mandatory=False)
    pairs = discovery._select(ref, cat)
    eq(len(pairs), 2, "both analyzers expanded")
    eq([r.id for r, _ in pairs], ["orb:config-extract", "orb:config-validate"], "child ids")
    ok(all(r.mandatory is False for r, _ in pairs), "children inherit mandatory")


@test("catalog")
async def catalog_missing_selection_drops_cleanly():
    cat = discovery.parse(ORB_CATALOG)
    ref = AnalyzerRef(id="x", api_url="https://vista.fortinet.com/orb",
                      catalog_select="does-not-exist")
    eq(discovery._select(ref, cat), [], "an unknown selection drops the analyzer, not the job")


# =========================================================================== #
# typing — the boolean default that used to break everything
# =========================================================================== #
@test("typing")
async def boolean_default_parses_and_survives():
    """`"default": true` used to raise ValidationError and drop the whole analyzer."""
    cat = discovery.parse(ORB_CATALOG)
    disc = [a for a in cat.analyzers if a.analyzer.id == "config-validate"][0]
    p = [x for x in disc.query.params if x.name == "check_lines"][0]
    eq(p.default, True, "boolean default preserved as a real bool")
    eq(p.coerce("true"), True, "string 'true' casts to True")
    eq(p.coerce("false"), False, "string 'false' casts to False")


@test("typing")
async def json_contract_gets_file_text_and_typed_values():
    cat = discovery.parse(ORB_CATALOG)
    disc = [a for a in cat.analyzers if a.analyzer.id == "config-validate"][0]
    ref = AnalyzerRef(id="config-validate", api_url="https://vista.fortinet.com/orb",
                      catalog_select="config-validate", mandatory=True)
    ai_plan = {"method": "POST", "url": "https://vista.fortinet.com/orb/config-validate/run",
               "content_type": "application/json", "file_param": "",
               "fields": {"config_snippet": "{{file_text}}", "version": "7.4.11",
                          "check_lines": "true"}}
    p = plan_mod.from_ai(ai_plan, ref, disc, "is this valid on 7.4.11?", "fgt.conf", SAMPLE_CONF)
    eq(p.fields["config_snippet"], SAMPLE_CONF, "{{file_text}} resolved to the config")
    eq(p.fields["version"], "7.4.11", "version carried through")
    eq(p.fields["check_lines"], True, "boolean cast from the declared type, not left a string")
    eq(p.source, "ai", "a fully valid AI plan needs no correction")


@test("typing")
async def deterministic_fills_json_body_from_file_text():
    """With the AI off, a body param named for the file's content still gets the content."""
    cat = discovery.parse(ORB_CATALOG)
    disc = [a for a in cat.analyzers if a.analyzer.id == "config-validate"][0]
    ref = AnalyzerRef(id="config-validate", api_url="x", mandatory=True)
    p = plan_mod.deterministic(ref, disc, "check it", "fgt.conf", SAMPLE_CONF)
    eq(p.fields["config_snippet"], SAMPLE_CONF, "config text filled deterministically")
    eq(p.fields["check_lines"], True, "advertised boolean default applied")
    ok("version" in p.fields, "required version present even with no value to give it")


@test("typing")
async def invented_params_are_still_rejected():
    """The safety rail must survive the typing changes."""
    cat = discovery.parse(ORB_CATALOG)
    disc = [a for a in cat.analyzers if a.analyzer.id == "config-validate"][0]
    ref = AnalyzerRef(id="config-validate", api_url="x", mandatory=True)
    p = plan_mod.from_ai(
        {"method": "GET", "url": "https://evil.example.com/run",
         "content_type": "application/json", "file_param": "",
         "fields": {"config_snippet": "{{file_text}}", "version": "7.4.11",
                    "made_up_param": "boom"}},
        ref, disc, "q", "f.conf", SAMPLE_CONF)
    eq(p.url, "https://vista.fortinet.com/orb/config-validate/run", "hostile url replaced")
    eq(p.method, "POST", "method corrected to the discovered one")
    ok("made_up_param" not in p.fields, "undeclared param dropped")
    eq(p.source, "ai-corrected", "corrections recorded")
    ok(any("evil.example.com" in n for n in p.notes), "the rejection is explained in notes")


# =========================================================================== #
# render — results with no report_markdown
# =========================================================================== #
@test("render")
async def orb_result_renders_without_ai():
    md = report.render(ORB_VALIDATE_RESULT, analyzer_id="config-validate",
                       title="FortiGate Config Validation")
    ok(md.startswith("## FortiGate Config Validation"), "heading present")
    ok("Validated against FortiOS 7.4.11" in md, "reasoning carried into the report")
    ok("Lines validated" in md or "Validated" in md, "counts rendered")
    ok("Balanced" in md, "structural block rendered")


@test("render")
async def native_markdown_always_wins():
    md, src = await report.normalize({"report_markdown": "## Real Report\n\nbody"},
                                     analyzer_id="logv", title="LogV")
    eq(src, "native", "native markdown must be used verbatim")
    eq(md, "## Real Report\n\nbody", "and not be rewritten")


@test("render")
async def snippets_render_as_fenced_config():
    md = report.render(
        {"analyzer": "config-extract", "ok": True, "reasoning": "Selected 1 section.",
         "result": {"snippets": [{"config_path": "router bgp", "text": SAMPLE_CONF}]}},
        analyzer_id="config-extract", title="FortiGate Config Extraction")
    ok("```" in md, "config is fenced")
    ok("set as 65001" in md, "config text present verbatim")
    ok("router bgp" in md, "config path labelled")


# =========================================================================== #
# live — real network
# =========================================================================== #
@test("live")
async def agentassist_gateway_answers():
    if not ai_model.ENABLED:
        raise Skip(f"AI intentionally off ({ai_model.DISABLED_REASON})")
    h = await ai_model.health()
    ok(h.get("ok"), f"gateway did not answer: {h.get('reason')}")


@test("live")
async def orb_discovery_is_reachable_and_is_a_catalog():
    doc, err = await discovery.probe("https://vista.fortinet.com/orb")
    ok(doc is not None, f"ORB discovery failed: {err}")
    ok(hasattr(doc, "analyzers"), "ORB /discover should be a catalog document")
    ids = sorted(a.analyzer.id for a in doc.analyzers)
    ok("config-validate" in ids and "config-extract" in ids, f"unexpected ORB routes: {ids}")


@test("live")
async def pipeline_validator_no_file():
    """A text-only tool, with the config AND the version living only in the question.

    This is the case the AI Controller exists for. ORB's ``version`` param is required and
    advertises no default, and no deterministic rule can pull "7.4.11" out of a sentence — so the
    two modes have genuinely different contracts, and both are asserted:

      AI on   the version reaches ORB and it validates.
      AI off  it degrades to a clean, explanatory error section — never a crash, and never a
              silently-wrong validation against a version nobody chose.
    """
    from orchestrator import pipeline
    rep = await pipeline.run(
        tool_name="FortiGate_Config_Validator", file_bytes=b"", filename="",
        question=("Is this valid on FortiOS 7.4.11?\n\n"
                  "config router bgp\n    set as 65001\n    set router-id 1.1.1.1\nend"))
    ok("No analyzers could be reached" not in rep, "analyzer must be reachable")
    ok("No analyzer produced a result" not in rep, "a section must be produced")

    if not ai_model.ENABLED:
        ok("FortiGate Config Validation" in rep, f"the section must still be titled; got:\n{rep}")
        ok("⚠️" in rep, f"AI-off must degrade to a visible error, not silence; got:\n{rep}")
        # With no file AND no AI, BOTH required params are empty (nothing can pull the config or
        # the version out of a sentence deterministically). ORB names whichever it checks first —
        # what matters is that the report names one, so the failure is actionable.
        ok(any(p in rep.lower() for p in ("version", "config_snippet")),
           f"the error should name the missing param so it is actionable; got:\n{rep}")
        return

    ok("7.4.11" in rep, f"the version must reach ORB; got:\n{rep[:600]}")
    ok(len(rep) > 120, f"report suspiciously short:\n{rep}")


@test("live")
async def pipeline_extractor_with_file():
    from orchestrator import pipeline
    rep = await pipeline.run(
        tool_name="FortiGate_Config_Extractor", file_bytes=SAMPLE_CONF.encode(),
        filename="fgt.conf", question="show my BGP config")
    ok("No analyzer produced a result" not in rep, "a section must be produced")
    ok("65001" in rep, f"the extracted config must appear; got:\n{rep[:600]}")


@test("live")
async def pipeline_logv_still_works():
    """The prod tool. Needs the LogV backend reachable; skipped if it is not."""
    from orchestrator import pipeline
    doc, err = await discovery.probe(
        "https://vista.fortinet.com/logVisualizer/api/agent_assist")
    if doc is None:
        raise Skip(f"LogV backend not reachable ({err})")
    sample = os.path.join(_REPO, "test_data", "sdwan-small.log")
    if not os.path.exists(sample):
        raise Skip("test_data/sdwan-small.log not present")
    with open(sample, "rb") as fh:
        data = fh.read()
    rep = await pipeline.run(tool_name="Log_Analyzer_Visualizer", file_bytes=data,
                             filename="sdwan-small.log",
                             question="What SLA failures happened on the Austin healthcheck?")
    ok("No analyzers could be reached" not in rep, "LogV must be reachable")
    ok(len(rep) > 200, f"LogV report suspiciously short:\n{rep[:400]}")


class Skip(Exception):
    """Raised by a test that cannot run in this environment."""


# =========================================================================== #
async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip tests that need the network")
    ap.add_argument("-k", default="", help="only run tests whose group/name contains this")
    ap.add_argument("-v", "--verbose", action="store_true", help="show full tracebacks")
    args = ap.parse_args()

    print(f"AI Controller: {ai_model.describe()}\n")
    width = max(len(n) for _, n, _ in _TESTS) + 2
    failures = 0
    current = ""
    for group, name, fn in _TESTS:
        if args.k and args.k not in group and args.k not in name:
            continue
        if group != current:
            current = group
            print(f"\n\033[1m{group}\033[0m")
        if args.offline and group == "live":
            print(f"  {SKIP} {name:<{width}} (offline)")
            _RESULTS.append((group, name, "skip"))
            continue
        t0 = time.time()
        try:
            await fn()
        except Skip as e:
            print(f"  {SKIP} {name:<{width}} {e}")
            _RESULTS.append((group, name, "skip"))
            continue
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  {FAIL} {name:<{width}} {type(e).__name__}: {e}")
            if args.verbose:
                traceback.print_exc()
            _RESULTS.append((group, name, "fail"))
            continue
        print(f"  {PASS} {name:<{width}} {int((time.time()-t0)*1000)}ms")
        _RESULTS.append((group, name, "pass"))

    p = sum(1 for *_, r in _RESULTS if r == "pass")
    s = sum(1 for *_, r in _RESULTS if r == "skip")
    print(f"\n{'─'*60}\n{p} passed · {failures} failed · {s} skipped")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
