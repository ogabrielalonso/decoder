from __future__ import annotations

import json
from pathlib import Path

from decoder.execution import MockExecutor, ModelTier, WorkerSpec
from decoder.execution.eval_harness import (
    BakeoffResult,
    Candidate,
    JudgeVerdict,
    QualityJudge,
    WorkerEval,
    count_citations,
    evaluate_candidate,
    format_matrix,
    has_sections,
    pick_winner,
    symbol_coverage,
)


def test_symbol_coverage() -> None:
    text = "uses `foo` and bar here"
    assert symbol_coverage(text, ["foo", "bar", "baz"]) == 2 / 3
    assert symbol_coverage(text, []) == 1.0


def test_count_citations() -> None:
    assert count_citations("see L3-L5 and L10 and L20-L40") == 3
    assert count_citations("no citations here") == 0


def test_has_sections() -> None:
    text = "### Purpose\nx\n### Notes\ny"
    assert has_sections(text, ["### Purpose", "### Notes"])
    assert not has_sections(text, ["### Purpose", "### Missing"])


def test_judge_parses_fenced_json() -> None:
    payload = '```json\n{"overall": 88, "accuracy": 90, "completeness": 85, "insight": 80, "hallucinations": 0, "notes": "solid"}\n```'
    judge = QualityJudge(MockExecutor(responder=lambda s: payload))
    v = judge.score(
        kind="alpha", reference_material="code", gold="gold",
        candidate="cand", source_dir=Path("/tmp"),
    )
    assert isinstance(v, JudgeVerdict)
    assert v.overall == 88
    assert v.accuracy == 90
    assert v.notes == "solid"


def test_judge_handles_garbage() -> None:
    judge = QualityJudge(MockExecutor(responder=lambda s: "not json"))
    v = judge.score(
        kind="alpha", reference_material="c", gold="g", candidate="x",
        source_dir=Path("/tmp"),
    )
    assert v.overall == 0
    assert "unparseable" in v.notes


def test_pick_winner_quality_first_then_cost() -> None:
    evals = [
        WorkerEval(worker_id="a", candidate="opus", quality=92, cost_usd=0.50),
        WorkerEval(worker_id="a", candidate="sonnet", quality=90, cost_usd=0.08),
        WorkerEval(worker_id="a", candidate="haiku", quality=70, cost_usd=0.01),
    ]
    # opus 92 is ceiling; sonnet 90 within epsilon=5 and cheaper -> wins
    winner = pick_winner(evals, epsilon=5)
    assert winner is not None
    assert winner.candidate == "sonnet"


def test_pick_winner_tight_epsilon_keeps_top() -> None:
    evals = [
        WorkerEval(worker_id="a", candidate="opus", quality=92, cost_usd=0.50),
        WorkerEval(worker_id="a", candidate="haiku", quality=70, cost_usd=0.01),
    ]
    # epsilon=1: only opus qualifies (haiku too far below) -> opus wins despite cost
    winner = pick_winner(evals, epsilon=1)
    assert winner.candidate == "opus"


def test_pick_winner_ignores_failures() -> None:
    evals = [
        WorkerEval(worker_id="a", candidate="codex", status="error", quality=0),
        WorkerEval(worker_id="a", candidate="haiku", quality=80, cost_usd=0.01),
    ]
    assert pick_winner(evals).candidate == "haiku"


def _judge_stub(score: int):
    payload = json.dumps(
        {"overall": score, "accuracy": score, "completeness": score,
         "insight": score, "hallucinations": 0, "notes": "ok"}
    )
    return QualityJudge(MockExecutor(responder=lambda s: payload))


def test_evaluate_candidate_full() -> None:
    spec = WorkerSpec(
        id="alpha-0", prompt="p", source_dir=Path("/tmp"),
        output_file=Path("/tmp/o.md"), scope=["a.py"],
    )
    cand = Candidate(
        "claude:haiku",
        MockExecutor(responder=lambda s: "### Purpose\nx\n`foo` at L3-L5"),
        ModelTier.CHEAP,
    )
    ev = evaluate_candidate(
        spec=spec, candidate=cand, judge=_judge_stub(85), kind="alpha",
        reference_material="code", gold="gold", symbol_names=["foo", "bar"],
        required_sections=["### Purpose"],
    )
    assert ev.status == "ok"
    assert ev.quality == 85
    assert ev.symbol_coverage == 0.5
    assert ev.citations == 1
    assert ev.sections_ok is True
    assert ev.cost_usd == 0.0


def test_format_matrix_flags_winner() -> None:
    result = BakeoffResult(evals=[
        WorkerEval(worker_id="alpha-0", candidate="opus", quality=92, cost_usd=0.5),
        WorkerEval(worker_id="alpha-0", candidate="haiku", quality=90, cost_usd=0.01),
    ])
    md = format_matrix(result)
    assert "Worker `alpha-0`" in md
    assert "🏆" in md
    assert "Winner: `haiku`" in md  # within epsilon, cheaper


def test_build_decomposed_alpha_prompt_closes_the_task() -> None:
    from decoder.schemas import SymbolRecord
    from decoder.synthesis.module_structure import build_decomposed_alpha_prompt

    syms = [
        SymbolRecord(file="a.py", language="python", kind="function", name="foo",
                     start_line=1, end_line=5),
    ]
    p = build_decomposed_alpha_prompt(["a.py"], syms, "/src")
    assert "`a.py`" in p
    assert "`foo`" in p  # symbol pre-listed (closed slot)
    assert '"purpose"' in p and '"risks"' in p  # closed JSON slots
    assert "no-error-handling" in p  # fixed risk enum (not open notes)
