from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from decoder import __version__
from decoder.config import TIER_LOC_BOUNDS, TIER_TEAM_PLAN, WORKER_CONTEXT_LIMIT, settings
from decoder.ingestion.cloner import _slug_from_url, resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.knowledge.graph_store import GraphStore, file_node_id
from decoder.knowledge.ingest import ingest_static_report
from decoder.knowledge.markdown_writer import ensure_modules_dir, write_index
from decoder.orchestration.budget import human_tokens
from decoder.orchestration.event_bus import Event, EventBus
from decoder.orchestration.master import compute_budget, plan_from_static
from decoder.qa.runner import run_qa, write_qa_artifacts
from decoder.schemas import StaticAnalysisReport
from decoder.static_analysis.pipeline import run_static_analysis
from decoder.synthesis.assembler import synthesize
from decoder.utils.logging import configure_logging, get_console, get_logger

app = typer.Typer(
    name="decoder",
    help="Decode GitHub repositories with hierarchical agent teams.",
    no_args_is_help=True,
    add_completion=False,
)

knowledge_app = typer.Typer(
    name="knowledge",
    help="Query the decoder knowledge stores (graph + vectors).",
    no_args_is_help=True,
)
app.add_typer(knowledge_app, name="knowledge")

qa_app = typer.Typer(
    name="qa",
    help="Quality assurance: coverage, validation, red-team prompt.",
    no_args_is_help=True,
)
app.add_typer(qa_app, name="qa")


@dataclass(slots=True)
class DecodeArtifacts:
    slug: str
    source_path: Path
    out_dir: Path
    static_report_path: Path
    plan_path: Path | None
    index_path: Path | None
    report: StaticAnalysisReport


@app.callback()
def _root(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging."),
) -> None:
    configure_logging("DEBUG" if verbose else settings.log_level)
    settings.ensure_dirs()


@app.command()
def version() -> None:
    """Show decoder version."""
    get_console().print(f"[bold cyan]decoder[/] {__version__}")


@app.command()
def info() -> None:
    """Show configuration and tier plan."""
    console = get_console()
    console.print(Panel.fit(f"decoder [cyan]{__version__}[/]", border_style="cyan"))

    cfg = Table(title="Configuration", show_header=True, header_style="bold magenta")
    cfg.add_column("Key")
    cfg.add_column("Value")
    cfg.add_row("workspace_dir", str(settings.workspace_dir))
    cfg.add_row("cache_dir", str(settings.cache_dir))
    cfg.add_row("output_dir", str(settings.output_dir))
    cfg.add_row("embedding_model", settings.embedding_model)
    cfg.add_row("log_level", settings.log_level)
    console.print(cfg)

    tiers = Table(title="Tier Plan", show_header=True, header_style="bold magenta")
    tiers.add_column("Tier")
    tiers.add_column("LOC range")
    tiers.add_column("Teams")
    tiers.add_column("Workers/team")
    for tier, (lo, hi) in TIER_LOC_BOUNDS.items():
        plan = TIER_TEAM_PLAN[tier]
        tiers.add_row(
            tier.value,
            f"{lo:,} - {hi:,}" if hi < 10**9 else f"{lo:,}+",
            str(plan["teams"]),
            str(plan["workers_per_team"]),
        )
    console.print(tiers)


@app.command()
def decode(
    source: str = typer.Argument(..., help="GitHub URL or local path to decode."),
    static_only: bool = typer.Option(
        False, "--static-only", help="Stop after static analysis; skip plan."
    ),
    no_history: bool = typer.Option(False, "--no-history", help="Skip git history analysis."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Override output directory for this decode."
    ),
    plan_only: bool = typer.Option(
        False, "--plan-only", help="Print the orchestration plan JSON to stdout and exit."
    ),
    index_vectors: bool = typer.Option(
        False,
        "--index-vectors",
        help="Also populate the vector store (downloads embedding model on first run).",
    ),
    workers: int | None = typer.Option(
        None, "--workers", help="Override Alpha worker count (bypass adaptive scaling)."
    ),
    target_tokens: int = typer.Option(
        120_000,
        "--target-tokens",
        "--alpha-target-tokens",  # alias: same name as `execute`/`eval-models`
        help="Target file-content tokens per Alpha worker (alias: --alpha-target-tokens); "
        "~60% of the 200k Sonnet worker window, leaving room for the injected-symbol "
        "prompt + JSON output. Adaptive scaling expands workers when exceeded.",
    ),
    alpha_max_workers: int | None = typer.Option(
        None,
        "--alpha-max-workers",
        help="Cap on Alpha workers when auto-chunking (lifts the default tier cap). "
        "Use with --target-tokens to get moderate chunks for the /decode Task-dispatch flow.",
    ),
    analyze_vendored: bool = typer.Option(
        False,
        "--analyze-vendored",
        help="Also fully analyze vendored/VCS dirs (node_modules, vendor, .git). "
        "Off by default: those are inventoried in assets.md (100% captured) without "
        "exploding the worker count. Turn on for literal whole-tree analysis.",
    ),
) -> None:
    """Decode a repository.

    Default flow: static/report.json, plan.json, index.md, modules/, graph store
    populated. Use --index-vectors to additionally index symbols in ChromaDB.
    """
    console = get_console()

    if plan_only:
        configure_logging("WARNING")

    artifacts = _run_decode(
        source,
        output=output,
        include_history=not no_history,
        quiet=plan_only,
    )

    if static_only:
        console.print(f"[green]static report written[/] {artifacts.static_report_path}")
        return

    plan = plan_from_static(
        artifacts.report,
        slug=artifacts.slug,
        output_root=artifacts.out_dir,
        static_report_path=artifacts.static_report_path,
        workers_override=workers,
        target_tokens_per_worker=target_tokens,
        max_workers=alpha_max_workers,
        analyze_vendored=analyze_vendored,
    )
    budget = compute_budget(plan)

    plan_path = artifacts.out_dir / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    ensure_modules_dir(artifacts.out_dir)
    index_path = write_index(artifacts.report, plan, artifacts.out_dir)

    artifacts.plan_path = plan_path
    artifacts.index_path = index_path

    # Populate graph store (always) and vector store (opt-in).
    graph_store = GraphStore(_graph_db_path())
    vector_store = None
    if index_vectors:
        from decoder.knowledge.embeddings import LocalEmbedder
        from decoder.knowledge.vector_store import VectorStore

        embedder = LocalEmbedder(settings.embedding_model)
        vector_store = VectorStore(settings.chroma_persist_dir, embedder)

    summary = ingest_static_report(
        artifacts.slug, artifacts.report, graph_store, vector_store=vector_store
    )

    bus = EventBus(artifacts.out_dir / "events.jsonl")
    bus.publish(
        Event(
            team="master",
            worker_id="master-0",
            type="plan.ready",
            payload={
                "slug": artifacts.slug,
                "tier": plan.tier.value,
                "teams": [t.id for t in plan.teams],
                "worker_counts": {t.id: len(t.workers) for t in plan.teams},
                "nodes": summary.nodes_written,
                "edges": summary.edges_written,
            },
        )
    )

    if plan_only:
        typer.echo(plan.model_dump_json(indent=2))
        return

    _render_plan(plan)
    _render_budget(budget)
    console.print(
        f"[green]graph[/] {summary.nodes_written} nodes, "
        f"{summary.edges_written} edges ingested"
    )
    if vector_store is not None:
        console.print(f"[green]vectors[/] {summary.vectors_written} symbols indexed")
    console.print(f"[green]plan written[/] {plan_path}")
    console.print(f"[green]index written[/] {index_path}")
    console.print(
        "\n[bold]Next:[/] run `/decode` inside Claude Code to dispatch the "
        "Team Alpha workers against this plan."
    )


@app.command("decode-compare")
def decode_compare(
    sources: list[str] = typer.Argument(
        ..., help="2 to 4 repositories (URL or path). Order matters for column layout."
    ),
    insights_prompt: bool = typer.Option(
        False,
        "--insights-prompt",
        help="Emit only the consolidated insights prompt to stdout (for the /decode-compare skill).",
    ),
    no_history: bool = typer.Option(True, "--history/--no-history", help="Skip git history analysis (default: skip)."),
) -> None:
    """Decode N repositories (2-4) and produce a unified N-way comparison.

    Runs static analysis per repo, builds the feature/structure matrices, and
    emits the Consolidated Insights Agent prompt. Full 4-team decodes of each
    repo are dispatched by the /decode-compare skill.
    """
    from decoder.compare.pipeline import MAX_REPOS, run_decode_compare

    if insights_prompt:
        configure_logging("WARNING")

    if not (2 <= len(sources) <= MAX_REPOS):
        raise typer.BadParameter(
            f"decode-compare accepts 2 to {MAX_REPOS} repositories; got {len(sources)}"
        )

    artifacts = run_decode_compare(sources, include_history=not no_history)

    if insights_prompt:
        prompt_text = artifacts.insights_prompt_file.read_text(encoding="utf-8")
        typer.echo(prompt_text)
        return

    console = get_console()
    console.print(f"[cyan]repos[/] {', '.join(artifacts.slugs)}")
    console.print(f"[cyan]compare root[/] {artifacts.compare_root}")
    console.print(f"[green]summary[/] {artifacts.matrix.summary}")
    console.print(f"[green]feature matrix[/] {artifacts.matrix.feature_matrix}")
    console.print(f"[green]structure matrix[/] {artifacts.matrix.structure_matrix}")
    console.print(f"[green]insights prompt[/] {artifacts.insights_prompt_file}")
    console.print(
        "\n[bold]Next:[/] run `/decode-compare` inside Claude Code to dispatch the "
        "full per-repo decodes + consolidated insights agent."
    )


@knowledge_app.command("status")
def knowledge_status(slug: str | None = typer.Argument(None)) -> None:
    """Show counts for the knowledge stores."""
    console = get_console()
    graph = GraphStore(_graph_db_path())
    counts = graph.counts(slug)
    console.print(
        Panel(
            f"slug: {slug or '<all>'}\n"
            f"graph: {counts['nodes']} nodes, {counts['edges']} edges\n"
            f"vector persist: {settings.chroma_persist_dir}",
            title="Knowledge Status",
            border_style="cyan",
        )
    )


@knowledge_app.command("search")
def knowledge_search(
    slug: str = typer.Argument(..., help="Repository slug to scope the search."),
    query: str = typer.Argument(..., help="Natural-language query."),
    k: int = typer.Option(5, "--k", help="Number of results."),
) -> None:
    """Semantic search over indexed symbols and module docs."""
    from decoder.knowledge.embeddings import LocalEmbedder
    from decoder.knowledge.vector_store import VectorStore

    embedder = LocalEmbedder(settings.embedding_model)
    store = VectorStore(settings.chroma_persist_dir, embedder)
    hits = store.search(query, slug=slug, k=k)

    console = get_console()
    if not hits:
        console.print("[yellow]no results[/]")
        return
    table = Table(title=f"Search: {query!r} (slug={slug})")
    table.add_column("score", justify="right")
    table.add_column("kind")
    table.add_column("where")
    table.add_column("text")
    for hit in hits:
        meta = hit.metadata
        where = f"{meta.get('file', '')}:{meta.get('start_line', '')}"
        table.add_row(
            f"{hit.score:.3f}",
            meta.get("kind", ""),
            where,
            hit.text[:120],
        )
    console.print(table)


@knowledge_app.command("neighbors")
def knowledge_neighbors(
    slug: str = typer.Argument(...),
    path: str = typer.Argument(..., help="Relative file path within the repo."),
    depth: int = typer.Option(1, "--depth", help="Neighborhood expansion depth."),
) -> None:
    """Show nodes connected to the given file within the graph store."""
    graph = GraphStore(_graph_db_path())
    node_id = file_node_id(slug, path)
    neighbors = graph.neighbors(node_id, depth=depth)
    console = get_console()
    if not neighbors:
        console.print(f"[yellow]no neighbors for {node_id}[/]")
        return
    for n in neighbors:
        console.print(f"- {n}")


def _graph_db_path() -> Path:
    return settings.cache_dir / "graph.sqlite"


def _load_static_report(slug: str) -> tuple[Path, StaticAnalysisReport]:
    out_dir = (settings.output_dir / "decode" / slug).resolve()
    report_path = out_dir / "static" / "report.json"
    if not report_path.exists():
        raise typer.BadParameter(
            f"static report not found for slug={slug!r} at {report_path}. "
            "Run `decoder decode <source>` first."
        )
    report = StaticAnalysisReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    return out_dir, report


@qa_app.command("run")
def qa_run(slug: str = typer.Argument(..., help="Decode slug to audit.")) -> None:
    """Compute coverage + validation and emit qa_report (json + md)."""
    out_dir, report = _load_static_report(slug)
    qa = run_qa(slug, report, out_dir)
    artifacts = write_qa_artifacts(qa, out_dir)

    console = get_console()
    table = Table(title=f"QA: {slug}", show_header=True, header_style="bold magenta")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    c = qa.coverage
    table.add_row("file coverage", f"{c.files_mentioned}/{c.total_files} ({c.file_coverage:.0%})")
    table.add_row(
        "symbol coverage (listed)",
        f"{c.symbols_mentioned}/{c.total_symbols} ({c.symbol_coverage:.0%})",
    )
    table.add_row(
        "symbol description (depth)",
        f"{c.symbols_described}/{c.symbols_rendered} ({c.symbol_description_rate:.0%})",
    )
    table.add_row("module issues", str(len(qa.module_validation.issues)))
    table.add_row("team output issues", str(len(qa.team_validation.issues)))
    table.add_row("warnings", str(len(qa.warnings)))
    console.print(table)
    console.print(f"[green]qa report[/] {artifacts['markdown']}")
    console.print(f"[green]qa json[/] {artifacts['json']}")
    console.print(
        f"[dim]Red-team prompt available at qa/qa_report.json -> .red_team.prompt; "
        f"the /decode skill dispatches the red-team worker and writes "
        f"{qa.red_team_output_file}.[/]"
    )


@app.command("synthesize")
def synthesize_cmd(
    slug: str = typer.Argument(..., help="Decode slug to synthesize."),
    executive_prompt: bool = typer.Option(
        False,
        "--executive-prompt",
        help="Emit only the executive summary prompt to stdout (for the /decode skill).",
    ),
) -> None:
    """Assemble api_catalog.md + final_report.md. Emits the executive prompt for the skill."""
    if executive_prompt:
        configure_logging("WARNING")
    out_dir, report = _load_static_report(slug)
    artifacts = synthesize(slug, report, out_dir)

    if executive_prompt:
        typer.echo(artifacts.executive_prompt)
        return

    console = get_console()
    console.print(f"[green]final report[/] {artifacts.final_report}")
    console.print(f"[green]api catalog[/] {artifacts.api_catalog}")
    console.print(
        f"[dim]Executive summary prompt ready; run "
        f"`decoder synthesize {slug} --executive-prompt` to pipe into a Task tool call. "
        f"Save its output to {artifacts.executive_output_file}.[/]"
    )


@app.command()
def budget(slug: str = typer.Argument(..., help="Decode slug to estimate.")) -> None:
    """Show the estimated token budget for an existing decode plan."""
    out_dir = (settings.output_dir / "decode" / slug).resolve()
    plan_path = out_dir / "plan.json"
    if not plan_path.exists():
        raise typer.BadParameter(f"plan.json not found at {plan_path}")
    from decoder.orchestration.contracts import OrchestrationPlan

    plan = OrchestrationPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    _render_budget(compute_budget(plan))


@qa_app.command("red-team-prompt")
def qa_red_team_prompt(slug: str = typer.Argument(...)) -> None:
    """Emit only the red-team prompt to stdout (consumed by the /decode skill)."""
    configure_logging("WARNING")
    out_dir, report = _load_static_report(slug)
    qa = run_qa(slug, report, out_dir)
    typer.echo(qa.red_team_prompt)


def _run_decode(
    source: str, *, output: Path | None, include_history: bool, quiet: bool = False
) -> DecodeArtifacts:
    console = get_console()
    logger = get_logger("decoder.cli")

    logger.info("decode: source=%s", source)
    src = resolve_source(source)
    slug = _slug_from_url(source) if src.kind == "git" else src.path.name
    out_dir = (output or (settings.output_dir / "decode" / slug)).resolve()
    static_dir = out_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    if not quiet:
        console.print(f"[cyan]source[/] {src.path}")
        if src.origin_url:
            console.print(f"[cyan]origin[/] {src.origin_url}")
        if src.commit:
            console.print(f"[cyan]commit[/] {src.commit[:12]}")

    metrics = compute_metrics(src)
    tier = decide_tier(metrics)

    if not quiet:
        _render_metrics(metrics)
        _render_tier(tier)

    report = run_static_analysis(src.path, metrics, tier, include_history=include_history)

    report_path = static_dir / "report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    return DecodeArtifacts(
        slug=slug,
        source_path=src.path,
        out_dir=out_dir,
        static_report_path=report_path,
        plan_path=None,
        index_path=None,
        report=report,
    )


def _render_metrics(metrics) -> None:
    console = get_console()
    table = Table(title="Repository Metrics", show_header=True, header_style="bold magenta")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("total files", f"{metrics.total_files:,}")
    table.add_row("ignored files", f"{metrics.ignored_files:,}")
    table.add_row("analyzed files", f"{metrics.analyzed_files:,}")
    table.add_row("total lines", f"{metrics.total_lines:,}")
    table.add_row("total bytes", f"{metrics.total_bytes:,}")
    table.add_row("estimated tokens", f"{metrics.estimated_tokens:,}")
    console.print(table)

    if metrics.languages:
        langs = Table(title="Languages", show_header=True, header_style="bold magenta")
        langs.add_column("Language")
        langs.add_column("Files", justify="right")
        langs.add_column("Lines", justify="right")
        for name, stats in sorted(metrics.languages.items(), key=lambda kv: -kv[1].lines):
            langs.add_row(name, f"{stats.files:,}", f"{stats.lines:,}")
        console.print(langs)


def _render_tier(tier) -> None:
    console = get_console()
    console.print(
        Panel(
            f"[bold cyan]tier[/] {tier.tier.value}\n"
            f"[dim]{tier.reasoning}[/]\n"
            f"teams={tier.teams}  workers_per_team={tier.workers_per_team}",
            title="Tier Decision",
            border_style="green",
        )
    )


def _render_budget(budget) -> None:
    console = get_console()
    t = Table(title="Budget Estimate (Claude tokens)", show_header=True, header_style="bold magenta")
    t.add_column("Team/Worker")
    t.add_column("Input", justify="right")
    t.add_column("Output", justify="right")
    t.add_column("Total", justify="right")
    for w in budget.workers:
        t.add_row(
            f"{w.team} / {w.worker_id}",
            human_tokens(w.input_tokens),
            human_tokens(w.output_tokens),
            human_tokens(w.total),
        )
    t.add_row(
        "[bold]TOTAL[/]",
        f"[bold]{human_tokens(budget.total_input)}[/]",
        f"[bold]{human_tokens(budget.total_output)}[/]",
        f"[bold]{human_tokens(budget.total)}[/]",
    )
    console.print(t)

    # Context-window safety: workers run on Sonnet (~200k). Flag any worker whose
    # estimated per-call context (input+output, before the injected-symbol prompt)
    # is already close to the window: a sign chunks are too big for the worker model.
    safe = int(WORKER_CONTEXT_LIMIT * 0.7)
    over = [w for w in budget.workers if w.total > safe]
    if over:
        worst = max(over, key=lambda w: w.total)
        console.print(
            f"[yellow]⚠ {len(over)} worker(s) estimated above {human_tokens(safe)} "
            f"(~70% of the {human_tokens(WORKER_CONTEXT_LIMIT)} Sonnet worker window); "
            f"worst {worst.worker_id} ≈ {human_tokens(worst.total)}. Lower --target-tokens "
            f"so each worker fits its window (prompt + files + output).[/]"
        )


def _render_plan(plan) -> None:
    console = get_console()
    for team in plan.teams:
        t = Table(
            title=f"Team {team.id} - {team.name}",
            show_header=True,
            header_style="bold magenta",
        )
        t.add_column("Worker")
        t.add_column("Files", justify="right")
        t.add_column("Tokens", justify="right")
        t.add_column("Output")
        for w in team.workers:
            t.add_row(
                w.id,
                str(len(w.scope)),
                f"{w.estimated_tokens:,}",
                Path(w.output_file).name,
            )
        console.print(t)


def main() -> None:
    app()


if __name__ == "__main__":
    main()


@app.command()
def execute(
    source: str = typer.Argument(..., help="GitHub URL or local path."),
    no_history: bool = typer.Option(False, "--no-history", help="Skip git history."),
    workers: int | None = typer.Option(None, "--workers", help="Override worker count."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output root override."
    ),
    max_parallel: int = typer.Option(3, "--max-parallel", help="Concurrent workers."),
    max_retries: int = typer.Option(
        2,
        "--max-retries",
        help="Same-tier (Sonnet) retries per worker when its output fails the "
        "deterministic contract, before escalating to the STRONG model. More "
        "cheap Sonnet retries fill omitted purposes so opus rarely fires.",
    ),
    alpha_target_tokens: int = typer.Option(
        12000,
        "--alpha-target-tokens",
        help="Per-Alpha-worker token budget. The chunker auto-scales the worker "
        "count to hold this fixed at any repo size, so each subprocess's JSON "
        "output always stays small and complete (no truncation / skeletons).",
    ),
    alpha_max_workers: int = typer.Option(
        2000,
        "--alpha-max-workers",
        help="Safety ceiling on auto-scaled Alpha workers (covers ~1M+ LOC at the "
        "fixed chunk size). The default rarely binds; chunk size, not this, drives "
        "granularity.",
    ),
    analyze_vendored: bool = typer.Option(
        False,
        "--analyze-vendored",
        help="Also analyze vendored/VCS dirs; off by default (inventoried in assets.md).",
    ),
    synth_executor: str = typer.Option(
        "claude", "--synth-executor", help="Synthesis executor: claude|codex."
    ),
) -> None:
    """Decode AND execute end-to-end: run the agent teams as CLI subprocesses.

    Token-efficient path: every team runs at the BALANCED tier (sonnet-grade) on
    local AI CLIs instead of the session model. Alpha runs on the primary
    (claude) executor; `--synth-executor codex` offloads ONLY synthesis to codex.
    """
    from decoder.execution.claude_cli import ClaudeCliExecutor
    from decoder.execution.codex_cli import CodexCliExecutor
    from decoder.execution.runner import default_routing, execute_plan

    console = get_console()
    artifacts = _run_decode(source, output=output, include_history=not no_history)
    plan = plan_from_static(
        artifacts.report,
        slug=artifacts.slug,
        output_root=artifacts.out_dir,
        static_report_path=artifacts.static_report_path,
        workers_override=workers,
        target_tokens_per_worker=alpha_target_tokens,
        max_workers=alpha_max_workers,
        analyze_vendored=analyze_vendored,
    )
    plan_path = artifacts.out_dir / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    ensure_modules_dir(artifacts.out_dir)

    cheap = ClaudeCliExecutor()
    balanced = (
        CodexCliExecutor() if synth_executor == "codex" else ClaudeCliExecutor()
    )
    routing = default_routing(cheap, balanced)

    n_workers = sum(len(t.workers) for t in plan.teams)
    routed = {t.id: routing[t.id] for t in plan.teams if t.id in routing}
    summary = ", ".join(
        f"{tid}->{r.executor.name}/{r.tier.value}" for tid, r in routed.items()
    )
    console.print(
        f"[bold]Executing[/] {artifacts.slug} (tier {plan.tier.value}) - "
        f"{n_workers} worker(s); routing: {summary}"
    )
    result = execute_plan(
        plan,
        artifacts.report,
        routing,
        max_parallel=max_parallel,
        max_retries=max_retries,
        log=console.print,
    )

    write_index(artifacts.report, plan, artifacts.out_dir)
    console.print(
        f"\n[green]Done[/]: {len(result.written)} file(s) written, "
        f"{len(result.failures)} failure(s), est. cost ${result.total_cost_usd:.4f}"
    )
    for f in result.failures:
        console.print(f"  [red]FAIL[/] {f.id}: {f.error}")
    console.print(f"Entry point: {artifacts.out_dir / 'index.md'}")


@app.command("assemble-alpha")
def assemble_alpha(
    slug: str = typer.Argument(..., help="Decode slug whose Alpha workers were dispatched."),
) -> None:
    """Assemble Alpha module docs from each worker's saved JSON return.

    The /decode in-conversation flow dispatches Alpha workers as in-session Task
    subagents that return the decomposed JSON; it saves each return next to the
    worker's output_file as `<name>.raw.json`. This merges those descriptions with
    the code-owned structure (symbols + imports from the static report) into the
    final module `.md`: the SAME assembly `decoder execute` does internally, so
    the structure is guaranteed and quality is identical. Missing/empty JSON yields
    a skeleton (structure only), never a lost module.
    """
    from decoder.orchestration.contracts import OrchestrationPlan
    from decoder.synthesis.module_structure import (
        assemble_decomposed_doc,
        parse_decomposed_alpha,
    )

    out_dir, report = _load_static_report(slug)
    plan_path = out_dir / "plan.json"
    if not plan_path.exists():
        raise typer.BadParameter(f"plan.json not found at {plan_path}")
    plan = OrchestrationPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    alpha = next((t for t in plan.teams if t.id == "alpha"), None)
    console = get_console()
    if alpha is None:
        console.print("[yellow]no alpha team in plan[/]")
        return

    assembled = skeletons = 0
    for w in alpha.workers:
        out = Path(w.output_file)
        # Tolerate any raw-path convention the dispatcher might use, so a fuzzy
        # skill instruction can't silently break assembly: with_suffix
        # ('alpha-worker-00.raw.json'), append ('alpha-worker-00.md.raw.json'),
        # or a plain '.json'. First existing match wins.
        raw = next(
            (
                c
                for c in (
                    out.with_suffix(".raw.json"),
                    Path(f"{out}.raw.json"),
                    out.with_suffix(".json"),
                )
                if c.is_file()
            ),
            None,
        )
        analyses = parse_decomposed_alpha(raw.read_text(encoding="utf-8")) if raw else {}
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            assemble_decomposed_doc(w.scope, report.symbols, report.imports, analyses),
            encoding="utf-8",
        )
        if analyses:
            assembled += 1
        else:
            skeletons += 1
    console.print(
        f"[green]assembled[/] {assembled} module doc(s)"
        + (f"; [yellow]{skeletons} skeleton-only[/] (missing/empty JSON)" if skeletons else "")
    )


@app.command("build-context-pack")
def build_context_pack_cmd(
    slug: str = typer.Argument(..., help="Decode slug whose modules to condense."),
) -> None:
    """Condense modules/ into context_pack.md (compact overview for synthesis teams).

    Run after assemble-alpha, before dispatching Bravo/Charlie/Delta. Synthesis
    teams read this instead of re-reading every module doc (big token saving on
    medium/large repos); they can still drill into modules/<file>.md for a specific
    file's full detail.
    """
    from decoder.synthesis.context_pack import build_context_pack

    out_dir = (settings.output_dir / "decode" / slug).resolve()
    modules_dir = out_dir / "modules"
    if not modules_dir.is_dir():
        raise typer.BadParameter(f"modules/ not found at {modules_dir}")
    pack = build_context_pack(modules_dir)
    (out_dir / "context_pack.md").write_text(pack, encoding="utf-8")
    get_console().print(
        f"[green]context pack[/] {out_dir / 'context_pack.md'} ({len(pack):,} chars)"
    )


@app.command("dump-prompts")
def dump_prompts(
    slug: str = typer.Argument(..., help="Decode slug whose plan to dump worker prompts from."),
    teams: str = typer.Option(
        None, "--teams", help="Comma-separated team ids to include (default: all)."
    ),
) -> None:
    """Write each worker's fully-injected prompt to prompts/<id>.txt and emit a
    manifest the /decode skill feeds to the decode-execute Workflow.

    Keeps the (large, symbol-injected) prompts on disk so the dispatch layer never
    has to carry them through context. The manifest (prompts/manifest.json) lists,
    per worker: id, team, prompt file, the output file to WRITE, and kind: `alpha` (write the decomposed JSON to <name>.raw.json, assembled later by
    assemble-alpha) or `synth` (write the markdown straight to architecture.md /
    domain.md / audit.md).
    """
    import json
    from collections import Counter

    from decoder.orchestration.contracts import OrchestrationPlan

    out_dir = (settings.output_dir / "decode" / slug).resolve()
    plan_path = out_dir / "plan.json"
    if not plan_path.exists():
        raise typer.BadParameter(f"plan.json not found at {plan_path}. Run `decoder decode` first.")
    plan = OrchestrationPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))

    wanted = {t.strip() for t in teams.split(",")} if teams else None
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    for team in plan.teams:
        if wanted is not None and team.id not in wanted:
            continue
        for w in team.workers:
            prompt_file = prompts_dir / f"{w.id}.txt"
            prompt_file.write_text(w.prompt, encoding="utf-8")
            out = Path(w.output_file)
            if team.id == "alpha":
                output_path, kind = str(out.with_suffix(".raw.json")), "alpha"
            else:
                output_path, kind = str(out), "synth"
            manifest.append(
                {
                    "id": w.id,
                    "team": team.id,
                    "prompt": str(prompt_file),
                    "output": output_path,
                    "kind": kind,
                }
            )

    (prompts_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    by_team: Counter[str] = Counter(m["team"] for m in manifest)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_team.items()))
    get_console().print(
        f"[green]dumped[/] {len(manifest)} worker prompt(s) ({summary}) -> {prompts_dir}\n"
        f"manifest: {prompts_dir / 'manifest.json'}"
    )


@app.command("cleanup")
def cleanup(
    slug: str = typer.Argument(..., help="Decode slug whose cloned source to remove."),
) -> None:
    """Delete the cloned source under workspace/ after a decode, keeping the output.

    The decode value lives in docs/decode/<slug>/ (self-contained markdown); the
    cloned repo in workspace/<slug>/ is just the means. This removes ONLY a clone
    we created: never a local path you decoded (source != the managed clone dir).
    """
    import shutil

    out_dir, report = _load_static_report(slug)
    source = Path(report.metrics.root).resolve()
    clone = (settings.workspace_dir / slug).resolve()
    console = get_console()
    if source == clone and clone.is_dir():
        shutil.rmtree(clone)
        console.print(
            f"[green]removed cloned source[/] {clone}\n"
            f"[dim]decode output kept at {out_dir}[/]"
        )
    else:
        console.print(
            f"[yellow]nothing removed[/]: source is a local path, not a managed "
            f"clone ({source}). The decode output is at {out_dir}."
        )


@app.command("doc-clean")
def doc_clean(
    path: Path = typer.Argument(..., help="Path to a session-dispatched doc to clean in place."),
) -> None:
    """Strip preamble/fences from a Task-dispatched doc (red_team.md / executive_summary.md).

    The /decode skill saves a Task agent's return verbatim; agents add preambles
    or code fences despite instructions. This applies the same deterministic gate
    the subprocess teams use, so the session path is code-guaranteed too.
    """
    from decoder.execution.runner import clean_doc

    if not path.is_file():
        raise typer.BadParameter(f"file not found: {path}")
    cleaned, ok = clean_doc(path.read_text(encoding="utf-8"))
    path.write_text(cleaned, encoding="utf-8")
    console = get_console()
    if ok:
        console.print(f"[green]cleaned[/] {path}")
    else:
        console.print(
            f"[yellow]warning[/]: {path} has no markdown body after cleaning "
            "(the agent likely narrated instead of producing the document)"
        )


# Candidate models for the bake-off. Edit this list to change who competes.
# (label, executor_kind, tier): opus listed FIRST so its output becomes the gold.
_EVAL_CANDIDATES = [
    ("claude:opus", "claude", "strong"),
    ("claude:sonnet", "claude", "balanced"),
    ("claude:haiku", "claude", "cheap"),
    ("codex:gpt-5.5", "codex", "balanced"),
]


@app.command("eval-models")
def eval_models(
    source: str = typer.Argument(..., help="GitHub URL or local path."),
    no_history: bool = typer.Option(True, "--history/--no-history"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    alpha_target_tokens: int = typer.Option(
        12000, "--alpha-target-tokens", help="Per-Alpha-worker token budget (finer = smaller chunks)."
    ),
    alpha_max_workers: int = typer.Option(
        60, "--alpha-max-workers", help="Cap on Alpha workers (lift to allow fine chunking)."
    ),
    runs: int = typer.Option(
        3, "--runs", help="Repeat each candidate N times and average (beats judge variance)."
    ),
    alpha_prompt: str = typer.Option(
        "optionc", "--alpha-prompt", help="Alpha task style: optionc|decomposed."
    ),
) -> None:
    """Bake-off: run each candidate model on Alpha + Bravo workers, judge quality
    against the Opus gold (source-grounded), and emit a ranked matrix.

    Quality is the gate; cost/time only break ties among Opus-grade results.
    Run this to DECIDE the routing defaults by evidence (do not guess).
    """
    from decoder.execution.base import ModelTier, WorkerSpec
    from decoder.execution.claude_cli import ClaudeCliExecutor
    from decoder.execution.codex_cli import CodexCliExecutor
    from decoder.execution.eval_harness import (
        BakeoffResult,
        Candidate,
        QualityJudge,
        aggregate_evals,
        evaluate_candidate,
        format_matrix,
    )
    from decoder.synthesis.module_structure import build_decomposed_alpha_prompt

    console = get_console()
    artifacts = _run_decode(source, output=output, include_history=not no_history)
    plan = plan_from_static(
        artifacts.report, slug=artifacts.slug, output_root=artifacts.out_dir,
        static_report_path=artifacts.static_report_path,
        target_tokens_per_worker=alpha_target_tokens,
        max_workers=alpha_max_workers,
    )

    tiers = {"cheap": ModelTier.CHEAP, "balanced": ModelTier.BALANCED, "strong": ModelTier.STRONG}

    def _exec(kind: str):
        return ClaudeCliExecutor() if kind == "claude" else CodexCliExecutor()

    candidates = [
        Candidate(label, _exec(kind), tiers[tier]) for label, kind, tier in _EVAL_CANDIDATES
    ]
    judge = QualityJudge(ClaudeCliExecutor())  # Opus judge (STRONG by default)

    alpha = next((t for t in plan.teams if t.id == "alpha"), None)
    bravo = next((t for t in plan.teams if t.id == "bravo"), None)

    def _symbol_density(worker) -> int:
        scope = set(worker.scope)
        return sum(1 for s in artifacts.report.symbols if s.file in scope)

    targets = []
    if alpha and alpha.workers:
        # Test the most code-dense Alpha chunk, not workers[0] (which is often
        # README/config with 0 symbols: a trivial, misleading test).
        densest = max(alpha.workers, key=_symbol_density)
        targets.append(("alpha", densest))
    if bravo and bravo.workers:
        targets.append(("bravo", bravo.workers[0]))

    src_dir = Path(plan.source_path)
    result = BakeoffResult()

    for kind, worker in targets:
        scope_syms = [
            (s.qualified_name or s.name)
            for s in artifacts.report.symbols
            if s.file in set(worker.scope)
        ]
        if kind == "alpha" and alpha_prompt == "decomposed":
            wprompt = build_decomposed_alpha_prompt(
                list(worker.scope), artifacts.report.symbols, plan.source_path
            )
            required = ["purpose"]
        elif kind == "alpha":
            wprompt = worker.prompt
            required = ["### Purpose"]
        else:
            wprompt = worker.prompt
            required = ["# Architecture"]
        # reference material grounding the judge
        if kind == "alpha":
            ref = "\n\n".join(
                f"# {p}\n{(src_dir / p).read_text(encoding='utf-8', errors='replace')[:4000]}"
                for p in worker.scope
                if (src_dir / p).is_file()
            )
        else:
            ref = artifacts.static_report_path.read_text(encoding="utf-8")[:8000]

        base_spec = WorkerSpec(
            id=worker.id, prompt=wprompt, source_dir=src_dir,
            output_file=Path(worker.output_file), scope=list(worker.scope),
            extra_read_dirs=[] if kind == "alpha" else [artifacts.out_dir],
        )

        # 1) Establish the Opus GOLD output (the reference the judge compares to).
        console.print(f"[dim]{kind}: establishing Opus gold...[/]")
        gold_spec = base_spec.model_copy(update={"tier": ModelTier.STRONG})
        gold_res = ClaudeCliExecutor().execute(gold_spec)
        gold = gold_res.output_text if gold_res.ok else "(opus gold unavailable)"

        # 2) Score every candidate against the gold (Opus included = calibration).
        for cand in candidates:
            console.print(f"[dim]{kind}: running {cand.label} (x{runs})...[/]")
            cand_runs = [
                evaluate_candidate(
                    spec=base_spec, candidate=cand, judge=judge, kind=kind,
                    reference_material=ref, gold=gold,
                    symbol_names=scope_syms, required_sections=required,
                )
                for _ in range(runs)
            ]
            result.evals.append(aggregate_evals(cand_runs))

    out_dir = artifacts.out_dir / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    matrix_path = out_dir / "bakeoff.md"
    matrix_path.write_text(format_matrix(result), encoding="utf-8")
    console.print(f"\n[green]Bake-off matrix:[/] {matrix_path}")
    console.print(format_matrix(result))
