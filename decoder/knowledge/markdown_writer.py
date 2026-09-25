from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from decoder.orchestration.contracts import OrchestrationPlan
from decoder.schemas import StaticAnalysisReport


def write_index(
    report: StaticAnalysisReport,
    plan: OrchestrationPlan,
    output_root: Path,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    index_path = output_root / "index.md"

    lines: list[str] = []
    lines.append(f"# Decode: {plan.slug}")
    lines.append("")
    lines.append(f"_Generated {datetime.now(UTC).isoformat(timespec='seconds')}._")
    lines.append("")

    lines.append("## Source")
    lines.append(f"- path: `{plan.source_path}`")
    if plan.origin_url:
        lines.append(f"- origin: {plan.origin_url}")
    if plan.commit:
        lines.append(f"- commit: `{plan.commit[:12]}`")
    if plan.branch:
        lines.append(f"- branch: `{plan.branch}`")
    lines.append("")

    lines.append("## Metrics")
    m = report.metrics
    lines.append(f"- analyzed files: **{m.analyzed_files:,}** (of {m.total_files:,} total)")
    lines.append(f"- total lines: **{m.total_lines:,}**")
    lines.append(f"- total bytes: {m.total_bytes:,}")
    lines.append(f"- estimated tokens: {m.estimated_tokens:,}")
    lines.append("")

    if m.languages:
        lines.append("### Languages")
        lines.append("")
        lines.append("| Language | Files | Lines | Bytes |")
        lines.append("|---|---:|---:|---:|")
        for name, stats in sorted(m.languages.items(), key=lambda kv: -kv[1].lines):
            lines.append(
                f"| {name} | {stats.files:,} | {stats.lines:,} | {stats.bytes:,} |"
            )
        lines.append("")

    lines.append("## Tier")
    lines.append(f"- tier: **{report.tier.tier.value}**")
    lines.append(f"- plan: {report.tier.teams} team(s) x {report.tier.workers_per_team} worker(s)")
    lines.append(f"- reasoning: {report.tier.reasoning}")
    lines.append("")

    lines.append("## Teams")
    if not plan.teams:
        lines.append("- _(nano tier: no teams; single-agent decode)_")
    for team in plan.teams:
        lines.append(f"### {team.name} (`{team.id}`)")
        lines.append("")
        for worker in team.workers:
            scope_preview = ", ".join(worker.scope[:3])
            if len(worker.scope) > 3:
                scope_preview += f" ... (+{len(worker.scope) - 3} more)"
            lines.append(f"- **{worker.id}** ({len(worker.scope)} files, "
                         f"~{worker.estimated_tokens:,} tokens) -> "
                         f"[`{Path(worker.output_file).name}`](modules/{Path(worker.output_file).name})")
            lines.append(f"  - scope: {scope_preview}")
        lines.append("")

    if plan.notes:
        lines.append("## Notes")
        for note in plan.notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## Static analysis")
    lines.append(f"- symbols indexed: {len(report.symbols):,}")
    lines.append(f"- imports indexed: {len(report.imports):,}")
    lines.append(
        f"- dep graph: {report.dep_graph_stats.get('nodes', 0)} nodes, "
        f"{report.dep_graph_stats.get('edges', 0)} edges"
    )
    lines.append(f"- raw report: [`{Path(plan.static_report_path).name}`](static/report.json)")
    lines.append("")

    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def ensure_modules_dir(output_root: Path) -> Path:
    modules = output_root / "modules"
    modules.mkdir(parents=True, exist_ok=True)
    return modules
