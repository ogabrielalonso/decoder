from __future__ import annotations

import json
from pathlib import Path

from decoder.execution import MockExecutor, ModelTier
from decoder.execution.runner import (
    Route,
    _clean_synthesis_output,
    _looks_like_doc,
    _synthesis_complaints,
    _tier_ladder,
    clean_doc,
    default_routing,
    execute_plan,
    parse_alpha_narratives,
    strip_json_fences,
)
from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.orchestration.master import plan_from_static
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _good_alpha(spec):
    payload = {p: {"purpose": f"purpose of {p}", "symbols": {}, "risks": []} for p in spec.scope}
    return json.dumps(payload)


def _good_synth(spec_id):
    return (
        f"# {spec_id}\n\n## Overview\n\nA grounded synthesis that cites "
        f"`src/main.py`:L1-L9 and contains plenty of words so it clears the "
        f"minimum-body guard comfortably across the whole document body here.\n\n"
        f"## Layering\n\nMore detail referencing `src/app.ts`."
    )


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def _make_plan(tmp_path: Path):
    report = _make_report()
    plan = plan_from_static(
        report,
        slug="sample",
        output_root=tmp_path / "out",
        static_report_path=tmp_path / "out" / "static" / "report.json",
    )
    return plan, report


def test_strip_json_fences() -> None:
    assert strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_json_fences('{"a":1}') == '{"a":1}'
    assert strip_json_fences("  ```\n{}\n```  ") == "{}"


def test_parse_alpha_narratives_tolerant() -> None:
    text = '```json\n{"a.py": {"purpose": "P", "notes": ["n1", "n2"]}, "b.py": "bad"}\n```'
    nar = parse_alpha_narratives(text)
    assert "a.py" in nar
    assert nar["a.py"].purpose == "P"
    assert nar["a.py"].notes == ["n1", "n2"]
    # malformed entry skipped, not fatal
    assert "b.py" not in nar


def test_parse_alpha_narratives_coerces_nonlist_notes() -> None:
    nar = parse_alpha_narratives('{"a.py": {"purpose": "P", "notes": "single"}}')
    assert nar["a.py"].notes == ["single"]


def _responder(spec):
    # Alpha workers get JSON; synthesis teams get a contract-passing markdown doc.
    if spec.id.startswith("alpha"):
        payload = {
            p: {"purpose": f"purpose of {p}", "symbols": {}, "risks": []} for p in spec.scope
        }
        return "```json\n" + json.dumps(payload) + "\n```"
    return _good_synth(spec.id)


def test_execute_plan_writes_alpha_and_synthesis(tmp_path: Path) -> None:
    plan, report = _make_plan(tmp_path)
    ex = MockExecutor(responder=_responder)
    routing = default_routing(ex)

    out = execute_plan(plan, report, routing, max_parallel=2)

    assert not out.failures
    # every worker produced a file
    assert len(out.written) == sum(len(t.workers) for t in plan.teams)

    # Alpha module doc was assembled: deterministic structure + narrative merged
    alpha = next(t for t in plan.teams if t.id == "alpha")
    mod = Path(alpha.workers[0].output_file)
    assert mod.exists()
    doc = mod.read_text()
    assert "### Purpose" in doc
    assert "### Key symbols" in doc  # deterministic section present
    assert "purpose of " in doc  # narrative merged in

    # Synthesis team wrote its markdown verbatim
    bravo = next(t for t in plan.teams if t.id == "bravo")
    bravo_out = Path(bravo.workers[0].output_file)
    assert bravo_out.read_text().startswith("# bravo-0")


def test_execute_plan_records_failures(tmp_path: Path) -> None:
    plan, report = _make_plan(tmp_path)

    class Failing(MockExecutor):
        def execute(self, spec):
            r = super().execute(spec)
            r.status = "error"
            r.error = "boom"
            return r

    # Route a SYNTHESIS team: it has no static skeleton to fall back on, so an
    # infra error is recorded as a hard failure with no doc. (Alpha, by contrast,
    # degrades to its code-owned skeleton: covered in the infra/escalation tests.)
    out = execute_plan(plan, report, {"bravo": Route(Failing(), ModelTier.BALANCED)}, max_parallel=1)
    assert out.failures
    assert out.written == []


def test_clean_synthesis_strips_preamble() -> None:
    raw = "Now I will produce the markdown.\n\n# Architecture\n\nLayered app body."
    assert _clean_synthesis_output(raw).startswith("# Architecture")


def test_clean_synthesis_strips_enclosing_fence() -> None:
    raw = "```markdown\n# Audit\n\nSome findings here.\n```"
    cleaned = _clean_synthesis_output(raw)
    assert cleaned.startswith("# Audit") and "```" not in cleaned


def test_looks_like_doc_rejects_narration() -> None:
    # the real Bravo failure: a confirmation message, no heading, no body.
    narration = "architecture.md written with 57 lines covering all 6 sections."
    assert not _looks_like_doc(_clean_synthesis_output(narration))
    assert _looks_like_doc("# Architecture\n\n" + "word " * 30)


def test_tier_ladder_escalates_to_strong() -> None:
    assert _tier_ladder(ModelTier.BALANCED, 1) == [
        ModelTier.BALANCED,
        ModelTier.BALANCED,
        ModelTier.STRONG,
    ]
    assert _tier_ladder(ModelTier.BALANCED, 0) == [ModelTier.BALANCED, ModelTier.STRONG]
    # already STRONG -> no redundant escalation
    assert _tier_ladder(ModelTier.STRONG, 1) == [ModelTier.STRONG, ModelTier.STRONG]


def test_synthesis_complaints_gate() -> None:
    assert _synthesis_complaints("narration with no heading at all") == [
        "no markdown body (you narrated instead of producing the document)"
    ]
    assert _synthesis_complaints(_good_synth("bravo-0")) == []  # passes the contract


def test_contract_retries_then_recovers(tmp_path: Path) -> None:
    plan, report = _make_plan(tmp_path)

    def _flaky(spec):
        if spec.id.startswith("alpha"):
            return _good_alpha(spec)
        # first attempt narrates (rejected); the retry carries feedback -> obey
        if "REJECTED" in spec.prompt:
            return _good_synth(spec.id)
        return "architecture.md written, 57 lines, all 6 sections covered."

    ex = MockExecutor(responder=_flaky)
    out = execute_plan(plan, report, default_routing(ex), max_parallel=1, max_retries=1)

    assert not out.failures
    assert len(out.written) == sum(len(t.workers) for t in plan.teams)
    bravo = next(t for t in plan.teams if t.id == "bravo")
    assert Path(bravo.workers[0].output_file).read_text().startswith("# bravo-0")


def test_contract_exhausts_ladder_and_escalates_then_fails(tmp_path: Path) -> None:
    plan, report = _make_plan(tmp_path)

    def _always_narrate(spec):
        if spec.id.startswith("alpha"):
            return _good_alpha(spec)
        return "wrote the file, all good, nothing else to add here."

    ex = MockExecutor(responder=_always_narrate)
    out = execute_plan(plan, report, default_routing(ex), max_parallel=1, max_retries=1)

    failed_ids = {r.id for r in out.failures}
    assert any(i.startswith("bravo") for i in failed_ids)  # synthesis failed loudly
    # the final attempt escalated to STRONG
    bravo_tiers = {s.tier for s in ex.calls if s.id.startswith("bravo")}
    assert ModelTier.STRONG in bravo_tiers
    # contract failure is reported, not silently written
    assert all("contract not met" in (r.error or "") for r in out.failures)


def test_alpha_degrades_to_skeleton_instead_of_failing(tmp_path: Path) -> None:
    # Alpha contract never met (empty purposes) -> after retries+escalation it must
    # still write the deterministic skeleton, NOT lose the worker (fix A vs E).
    plan, report = _make_plan(tmp_path)

    def _empty_purpose_alpha(spec):
        if spec.id.startswith("alpha"):
            return json.dumps({p: {"purpose": "", "symbols": {}, "risks": []} for p in spec.scope})
        return _good_synth(spec.id)

    ex = MockExecutor(responder=_empty_purpose_alpha)
    out = execute_plan(plan, report, default_routing(ex), max_parallel=1, max_retries=1)

    alpha = next(t for t in plan.teams if t.id == "alpha")
    mod = Path(alpha.workers[0].output_file)
    assert mod.exists()  # skeleton written despite failing the contract
    doc = mod.read_text()
    assert "### Key symbols" in doc and "### Purpose" in doc  # deterministic structure kept
    # the Alpha worker is NOT reported as a failure (it produced a usable doc)
    assert not any(r.id.startswith("alpha") for r in out.failures)
    # but it did exhaust the ladder (escalated to STRONG before degrading)
    assert ModelTier.STRONG in {s.tier for s in ex.calls if s.id.startswith("alpha")}


def test_clean_doc_public_gate() -> None:
    # the exact red-team contamination: preamble + horizontal rule before the doc
    raw = "I now have all the files. Let me produce the review.\n\n---\n\n# Red Team Review\n\n" + "w " * 30
    cleaned, ok = clean_doc(raw)
    assert ok and cleaned.startswith("# Red Team Review")
    # narration with no real body is reported as not-a-doc
    _, ok2 = clean_doc("red_team.md written, all findings covered.")
    assert not ok2


def test_clean_doc_strips_orphaned_trailing_fence() -> None:
    # preamble BEFORE the opening ``` -> opener removed with preamble, closing
    # ``` would orphan. Must be stripped (audit finding).
    raw = "Sure, here is the analysis:\n\n```markdown\n# Red Team Review\n\nAll claims verified.\n```\n"
    cleaned, ok = clean_doc(raw)
    assert ok and cleaned.startswith("# Red Team Review")
    assert "```" not in cleaned


def test_clean_doc_keeps_legitimate_code_block() -> None:
    # a real doc ending in a fenced code block (balanced pair) must NOT lose it
    raw = "# Audit\n\n## Example\n\n```python\nfoo()\n```"
    cleaned, _ = clean_doc(raw)
    assert cleaned.count("```") == 2 and cleaned.rstrip().endswith("```")


def test_clean_doc_short_valid_doc_not_flagged() -> None:
    # short but well-formed doc must not trip the "narrated" warning (audit finding)
    _, ok = clean_doc("# Red Team Review\n\n## Summary\nAll claims verified.")
    assert ok


def test_alpha_contract_gates_every_file_and_names_missing() -> None:
    # The WHOLE repo matters: every file needs a purpose (docs/configs too), and
    # the complaint NAMES the missing files so the retry fills exactly those.
    from decoder.execution.runner import _alpha_complaints
    from decoder.synthesis.module_structure import FileAnalysis

    scope = ["README.md", "core-config.yaml", "main.py"]
    empty = {f: FileAnalysis(purpose="") for f in scope}
    c = _alpha_complaints(scope, empty)
    assert c and "README.md" in c[0] and "core-config.yaml" in c[0]  # docs/configs named
    full = {f: FileAnalysis(purpose="does X") for f in scope}
    assert _alpha_complaints(scope, full) == []  # all covered -> passes


def test_infra_error_never_escalates_to_opus(tmp_path: Path) -> None:
    # A throttle/infra error (result not ok) must NOT escalate the model: piling
    # opus onto a rate-limited subscription is the death spiral we're preventing.
    plan, report = _make_plan(tmp_path)

    class Throttled(MockExecutor):
        def execute(self, spec):
            r = super().execute(spec)
            r.status = "error"
            r.error = "overloaded (simulated throttle)"
            return r

    ex = Throttled()
    out = execute_plan(plan, report, default_routing(ex), max_parallel=1, max_retries=1)

    # KEY: no worker was ever escalated to STRONG on an infra error
    assert ModelTier.STRONG not in {s.tier for s in ex.calls}
    # synthesis with no usable body fails; Alpha still writes its static skeleton
    failed_ids = {r.id for r in out.failures}
    assert any(i.startswith("bravo") for i in failed_ids)
    assert not any(i.startswith("alpha") for i in failed_ids)  # Alpha degraded, not failed
    alpha = next(t for t in plan.teams if t.id == "alpha")
    assert Path(alpha.workers[0].output_file).exists()  # skeleton written despite throttle


def test_default_routing_alpha_on_primary_executor() -> None:
    # --synth-executor codex must offload ONLY synthesis; Alpha stays on primary
    primary, synth = MockExecutor(), MockExecutor()
    routing = default_routing(primary, synth)
    assert routing["alpha"].executor is primary
    assert routing["bravo"].executor is synth
    assert routing["charlie"].executor is synth
    assert routing["delta"].executor is synth


def test_default_routing_tiers() -> None:
    ex = MockExecutor()
    routing = default_routing(ex)
    # haiku is below the quality floor on code; Alpha now uses BALANCED too.
    assert routing["alpha"].tier == ModelTier.BALANCED
    assert routing["bravo"].tier == ModelTier.BALANCED
    assert routing["delta"].tier == ModelTier.BALANCED
