from __future__ import annotations

from pathlib import Path

from decoder.config import (
    MAX_FILES_PER_WORKER,
    MAX_SYMBOLS_PER_WORKER,
    WORKER_CONTEXT_LIMIT,
    Tier,
)
from decoder.orchestration.budget import (
    BudgetReport,
    adapt_worker_count,
    estimate_plan_budget,
)
from decoder.orchestration.chunker import (
    Chunk,
    build_file_infos,
    chunk_files,
    render_asset_inventory,
)
from decoder.orchestration.contracts import (
    OrchestrationPlan,
    TeamPlan,
    WorkerAssignment,
)
from decoder.orchestration.prompts import (
    ALPHA_LEAD_PROMPT,
    BRAVO_WORKER_PROMPT,
    CHARLIE_WORKER_PROMPT,
    DELTA_WORKER_PROMPT,
)
from decoder.schemas import ImportEdge, StaticAnalysisReport, SymbolRecord
from decoder.static_analysis.graph_metrics import (
    render_entity_candidates,
    render_glossary_candidates,
    render_metrics_block,
)
from decoder.static_analysis.security_scan import render_security_hints, scan_security
from decoder.synthesis.module_structure import build_decomposed_alpha_prompt

_TOKENS_PER_BYTE = 0.25
# Workers run on Sonnet (~200k window: see WORKER_CONTEXT_LIMIT), NOT Opus's 1M.
# Target ~60% of the window for file CONTENT, leaving ~40% for the injected-symbol
# prompt + JSON output + reasoning overhead, so a worker never overflows its window.
_DEFAULT_TARGET_TOKENS_PER_WORKER = int(WORKER_CONTEXT_LIMIT * 0.6)  # 120k


def plan_from_static(
    report: StaticAnalysisReport,
    *,
    slug: str,
    output_root: Path,
    static_report_path: Path,
    workers_override: int | None = None,
    target_tokens_per_worker: int = _DEFAULT_TARGET_TOKENS_PER_WORKER,
    max_workers: int | None = None,
    analyze_vendored: bool = False,
) -> OrchestrationPlan:
    """Produce an OrchestrationPlan from a StaticAnalysisReport.

    All four teams (Alpha/Bravo/Charlie/Delta) are included and must run in
    declaration order: later teams read earlier teams' outputs.
    Alpha worker count is adaptive: the lib expands the baseline tier plan
    when each worker would exceed the token budget, unless `workers_override`
    is provided.
    """
    tier = report.tier.tier
    baseline = report.tier.workers_per_team if report.tier.teams > 0 else 1

    # Build the analyzable file set FIRST, then size workers from what is actually
    # chunked (text files, vendored excluded): not the whole-repo byte count, which
    # includes binaries/vendored that never become Alpha scope.
    source_root = report.metrics.root
    file_infos, assets = build_file_infos(
        source_root, report.symbols, analyze_vendored=analyze_vendored
    )
    chunked_bytes = sum(f.bytes for f in file_infos)
    chunked_symbols = sum(f.symbols for f in file_infos)

    if workers_override is not None:
        worker_count = max(1, workers_override)
    else:
        worker_count = adapt_worker_count(
            tier,
            baseline,
            chunked_bytes,
            max_tokens_per_worker=target_tokens_per_worker,
            total_files=len(file_infos),
            total_symbols=chunked_symbols,
            **({"max_workers": max_workers} if max_workers else {}),
        )

    metrics_block = render_metrics_block(report, source_root)
    entities_block = render_entity_candidates(report)
    # Charlie (domain) also gets the metrics block (long methods = where business
    # rules live; churn/coupling contextualize entities) + a deterministic glossary
    # skeleton (entity + method names) so it defines terms instead of hunting them.
    charlie_block = f"{entities_block}\n\n{render_glossary_candidates(report)}\n\n{metrics_block}"
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "assets.md").write_text(render_asset_inventory(assets), encoding="utf-8")
    chunks = chunk_files(
        file_infos,
        worker_count=worker_count,
        max_files_per_chunk=MAX_FILES_PER_WORKER,
        max_symbols_per_chunk=MAX_SYMBOLS_PER_WORKER,
    )

    modules_dir = output_root / "modules"
    languages_summary = _summarize_languages(report.metrics)

    alpha = TeamPlan(
        id="alpha",
        name="Code Understanding",
        domain="code",
        lead_prompt=ALPHA_LEAD_PROMPT.format(
            source_path=str(source_root),
            tier=tier.value,
            analyzed_files=report.metrics.analyzed_files,
            total_lines=report.metrics.total_lines,
            languages_summary=languages_summary,
            worker_count=len(chunks),
        ),
        workers=[
            _alpha_worker(
                worker_index=i,
                chunk=chunk,
                source_root=source_root,
                symbols=report.symbols,
                imports=report.imports,
                modules_dir=modules_dir,
            )
            for i, chunk in enumerate(chunks)
        ],
    )

    bravo = _single_worker_team(
        team_id="bravo",
        name="Architecture & Patterns",
        domain="architecture",
        prompt_template=BRAVO_WORKER_PROMPT,
        source_root=source_root,
        output_root=output_root,
        static_report_path=static_report_path,
        output_filename="architecture.md",
        metrics=metrics_block,
    )

    charlie = _single_worker_team(
        team_id="charlie",
        name="Business Domain",
        domain="domain",
        prompt_template=CHARLIE_WORKER_PROMPT,
        source_root=source_root,
        output_root=output_root,
        static_report_path=static_report_path,
        output_filename="domain.md",
        metrics=charlie_block,
    )

    delta = _single_worker_team(
        team_id="delta",
        name="Security, Performance, Quality",
        domain="audit",
        prompt_template=DELTA_WORKER_PROMPT,
        source_root=source_root,
        output_root=output_root,
        static_report_path=static_report_path,
        output_filename="audit.md",
        metrics=f"{metrics_block}\n\n{render_security_hints(scan_security(source_root))}",
    )

    teams = [t for t in (alpha, bravo, charlie, delta) if t.workers]
    notes = _build_notes(tier, report, len(chunks), baseline, worker_count)

    return OrchestrationPlan(
        slug=slug,
        source_path=str(source_root),
        origin_url=report.metrics.origin_url,
        commit=report.metrics.commit,
        branch=report.metrics.branch,
        tier=tier,
        teams=teams,
        output_root=str(output_root),
        static_report_path=str(static_report_path),
        notes=notes,
    )


def _alpha_worker(
    *,
    worker_index: int,
    chunk: Chunk,
    source_root: Path,
    symbols: list[SymbolRecord],
    imports: list[ImportEdge],
    modules_dir: Path,
) -> WorkerAssignment:
    scope = [f.relative for f in chunk.files]

    # Decomposed Alpha task: symbols are pre-listed (from static analysis) and
    # the worker fills closed slots. Validated to lift mid-tier models to
    # Opus-grade on code extraction (codex gpt-5.5 +13, sonnet +4 vs the open
    # task). See docs/design RFC §14+.
    prompt = build_decomposed_alpha_prompt(scope, symbols, str(source_root))

    output_file = modules_dir / f"alpha-worker-{worker_index:02d}.md"

    return WorkerAssignment(
        id=f"alpha-{worker_index}",
        scope=scope,
        estimated_bytes=chunk.total_bytes,
        estimated_tokens=int(chunk.total_bytes * _TOKENS_PER_BYTE),
        prompt=prompt,
        output_file=str(output_file),
    )


def _single_worker_team(
    *,
    team_id: str,
    name: str,
    domain: str,
    prompt_template: str,
    source_root: Path,
    output_root: Path,
    static_report_path: Path,
    output_filename: str,
    metrics: str = "",
) -> TeamPlan:
    prompt = prompt_template.format(
        source_path=str(source_root),
        output_root=str(output_root),
        static_report_path=str(static_report_path),
        metrics=metrics,
    )
    worker = WorkerAssignment(
        id=f"{team_id}-0",
        scope=[],
        estimated_bytes=0,
        estimated_tokens=0,
        prompt=prompt,
        output_file=str(output_root / output_filename),
    )
    return TeamPlan(
        id=team_id,
        name=name,
        domain=domain,
        lead_prompt=prompt,
        workers=[worker],
    )


def _summarize_languages(metrics) -> str:
    if not metrics.languages:
        return "none"
    top = sorted(metrics.languages.items(), key=lambda kv: -kv[1].lines)[:5]
    return ", ".join(f"{name} ({stats.lines:,} LOC)" for name, stats in top)


def _build_notes(
    tier: Tier,
    report: StaticAnalysisReport,
    chunk_count: int,
    baseline: int,
    adapted: int,
) -> list[str]:
    notes: list[str] = []
    if tier is Tier.NANO:
        notes.append(
            "nano tier: single Alpha worker; Bravo/Charlie/Delta still run for completeness."
        )
    notes.append(
        f"Team Alpha will dispatch {chunk_count} worker(s) covering "
        f"{report.metrics.analyzed_files} analyzed files."
    )
    if adapted != baseline:
        notes.append(
            f"adaptive scaling: baseline was {baseline} worker(s), "
            f"expanded to {adapted} to keep per-worker token budget in range."
        )
    notes.append("Teams run sequentially: Alpha -> Bravo -> Charlie -> Delta.")
    if report.metrics.estimated_tokens > 500_000:
        notes.append(
            "estimated tokens exceed 500k; verify per-worker budgets before dispatching."
        )
    return notes


def compute_budget(plan: OrchestrationPlan) -> BudgetReport:
    """Estimate the Claude budget to execute a given plan."""
    alpha = next((t for t in plan.teams if t.id == "alpha"), None)
    alpha_bytes = [w.estimated_bytes for w in alpha.workers] if alpha else []
    return estimate_plan_budget(
        plan.slug,
        plan.tier,
        alpha_bytes,
        target_tokens_per_worker=_DEFAULT_TARGET_TOKENS_PER_WORKER,
    )
