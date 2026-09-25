from pathlib import Path

from decoder.config import Tier
from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.orchestration.budget import adapt_worker_count, estimate_plan_budget, human_tokens
from decoder.orchestration.master import compute_budget, plan_from_static
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def test_adapt_worker_count_respects_baseline_when_budget_is_ample() -> None:
    # 10kb of code with baseline 3 workers and 120k-token budget fits easily.
    assert adapt_worker_count(Tier.MEDIUM, 3, 10_000, max_tokens_per_worker=120_000) == 3


def test_adapt_worker_count_expands_when_budget_exceeded() -> None:
    # 5 MB of code -> ~1.31M tokens / 120k budget -> 11 workers. The cap is now a
    # high backstop (300), so token-based expansion is no longer clipped at 7.
    result = adapt_worker_count(Tier.LARGE, 3, 5 * 1024 * 1024, max_tokens_per_worker=120_000)
    assert result == 11


def test_adapt_worker_count_is_file_bound_for_many_small_files() -> None:
    # Low byte count (token budget would say 1 worker) but 450 files -> the OUTPUT
    # cap of 100 files/worker forces 5 workers. This is the case a token-only count
    # missed: huge repos of many small files collapsing into one truncating worker.
    result = adapt_worker_count(
        Tier.HUGE,
        5,
        200_000,  # ~50k tokens -> token-based = 1
        max_tokens_per_worker=250_000,
        total_files=450,
        max_files_per_worker=100,
    )
    assert result == 5  # ceil(450 / 100)


def test_adapt_worker_count_nano_stays_at_one() -> None:
    assert adapt_worker_count(Tier.NANO, 1, 100_000, max_tokens_per_worker=120_000) == 1


def test_adapt_worker_count_respects_custom_max() -> None:
    # a raised/lowered max_workers still binds as a hard backstop
    n = adapt_worker_count(
        Tier.LARGE, 3, 5 * 1024 * 1024, max_tokens_per_worker=12_000, max_workers=60
    )
    assert n == 60  # token-based would be ~110, clipped to the explicit cap


def test_estimate_plan_budget_structure() -> None:
    budget = estimate_plan_budget(
        "sample", Tier.MEDIUM, alpha_worker_bytes=[10_000, 12_000], target_tokens_per_worker=120_000
    )
    team_ids = {w.team for w in budget.workers}
    assert team_ids == {"alpha", "bravo", "charlie", "delta", "red_team"}
    # 2 alpha + 3 synth + 1 red = 6 workers.
    assert len(budget.workers) == 6
    assert budget.total_input > 0
    assert budget.total_output > 0


def test_human_tokens_formats_ranges() -> None:
    assert human_tokens(42) == "42"
    assert human_tokens(1_500) == "1.5k"
    assert human_tokens(2_300_000) == "2.30M"


def test_plan_workers_override_wins(tmp_path: Path) -> None:
    report = _make_report()
    plan = plan_from_static(
        report,
        slug="sample",
        output_root=tmp_path / "out",
        static_report_path=tmp_path / "out" / "static" / "report.json",
        workers_override=4,
    )
    alpha = next(t for t in plan.teams if t.id == "alpha")
    # Override raises the worker count above the nano default (1). With close-before-
    # overshoot the actual count is a target, not exact: it may emit a few more to keep
    # each worker within its context window: but never more chunks than there are files.
    assert 1 < len(alpha.workers) <= 6


def test_compute_budget_for_real_plan(tmp_path: Path) -> None:
    report = _make_report()
    plan = plan_from_static(
        report,
        slug="sample",
        output_root=tmp_path / "out",
        static_report_path=tmp_path / "out" / "static" / "report.json",
    )
    budget = compute_budget(plan)
    assert budget.total > 0
    assert budget.slug == "sample"
