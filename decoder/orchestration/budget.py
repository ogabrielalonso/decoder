from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from decoder.config import (
    MAX_FILES_PER_WORKER,
    MAX_SYMBOLS_PER_WORKER,
    MAX_WORKERS_PER_TEAM,
    Tier,
)
from decoder.schemas import RepoMetrics

TOKENS_PER_BYTE = 0.25
OUTPUT_TOKENS_PER_ALPHA_WORKER = 1500
OUTPUT_TOKENS_PER_SYNTH_WORKER = 3500
OUTPUT_TOKENS_RED_TEAM = 4000
OVERHEAD_TOKENS_PER_CALL = 2500


@dataclass(slots=True)
class WorkerBudget:
    team: str
    worker_id: str
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class BudgetReport:
    slug: str
    tier: Tier
    target_tokens_per_worker: int
    workers: list[WorkerBudget] = field(default_factory=list)

    @property
    def total_input(self) -> int:
        return sum(w.input_tokens for w in self.workers)

    @property
    def total_output(self) -> int:
        return sum(w.output_tokens for w in self.workers)

    @property
    def total(self) -> int:
        return self.total_input + self.total_output


def adapt_worker_count(
    tier: Tier,
    baseline_workers: int,
    total_bytes: int,
    *,
    max_tokens_per_worker: int,
    total_files: int = 0,
    max_files_per_worker: int = MAX_FILES_PER_WORKER,
    total_symbols: int = 0,
    max_symbols_per_worker: int = MAX_SYMBOLS_PER_WORKER,
    max_workers: int = MAX_WORKERS_PER_TEAM,
) -> int:
    """Derive the Alpha worker count from the THREE constraints that bound a worker:

    - INPUT: tokens per worker <= ``max_tokens_per_worker`` (sized to the Sonnet
      worker window; see _DEFAULT_TARGET_TOKENS_PER_WORKER).
    - OUTPUT (files): files per worker <= ``max_files_per_worker``. The worker emits
      one JSON object covering every file, so output grows with file count.
    - OUTPUT (symbols): symbols per worker <= ``max_symbols_per_worker``. The worker
      also describes every symbol; when too many symbols pile up (dense "god module"
      files), the agent skimps and omits descriptions. This is the constraint a
      files+tokens-only count missed: symbols, not files, drive most of the output.

    Worker count is the max of the needs (never below baseline), capped at
    ``max_workers`` (a high backstop, not a real limit). Nano stays at baseline.
    """
    if total_bytes <= 0:
        return max(1, baseline_workers)
    if tier is Tier.NANO:
        return max(1, baseline_workers)

    baseline = max(1, baseline_workers)
    token_based = ceil((total_bytes * TOKENS_PER_BYTE) / max_tokens_per_worker)
    file_based = ceil(total_files / max_files_per_worker) if max_files_per_worker > 0 else 0
    symbol_based = ceil(total_symbols / max_symbols_per_worker) if max_symbols_per_worker > 0 else 0
    needed = max(baseline, token_based, file_based, symbol_based)
    return min(needed, max_workers)


def estimate_plan_budget(
    slug: str, tier: Tier, alpha_worker_bytes: list[int], target_tokens_per_worker: int
) -> BudgetReport:
    """Estimate total budget for the whole 4-team + red-team flow."""
    report = BudgetReport(slug=slug, tier=tier, target_tokens_per_worker=target_tokens_per_worker)

    for i, bytes_ in enumerate(alpha_worker_bytes):
        report.workers.append(
            WorkerBudget(
                team="alpha",
                worker_id=f"alpha-{i}",
                input_tokens=int(bytes_ * TOKENS_PER_BYTE) + OVERHEAD_TOKENS_PER_CALL,
                output_tokens=OUTPUT_TOKENS_PER_ALPHA_WORKER,
            )
        )

    # Bravo, Charlie, Delta each read all alpha outputs (roughly sum of alpha output)
    synth_input = OUTPUT_TOKENS_PER_ALPHA_WORKER * len(alpha_worker_bytes) + OVERHEAD_TOKENS_PER_CALL
    for team in ("bravo", "charlie", "delta"):
        report.workers.append(
            WorkerBudget(
                team=team,
                worker_id=f"{team}-0",
                input_tokens=synth_input,
                output_tokens=OUTPUT_TOKENS_PER_SYNTH_WORKER,
            )
        )

    # Red team reads all outputs.
    red_input = (
        OUTPUT_TOKENS_PER_ALPHA_WORKER * len(alpha_worker_bytes)
        + OUTPUT_TOKENS_PER_SYNTH_WORKER * 3
        + OVERHEAD_TOKENS_PER_CALL
    )
    report.workers.append(
        WorkerBudget(
            team="red_team",
            worker_id="red-0",
            input_tokens=red_input,
            output_tokens=OUTPUT_TOKENS_RED_TEAM,
        )
    )
    return report


def human_tokens(n: int) -> str:
    if n < 1000:
        return f"{n}"
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def budget_matches_metrics(metrics: RepoMetrics, target: int) -> bool:
    """Sanity check: ensure the target per worker is not absurdly larger than
    the entire repo, which would collapse the plan into a single worker."""
    return metrics.total_bytes * TOKENS_PER_BYTE > target // 4
