from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from decoder.schemas import StaticAnalysisReport
from decoder.synthesis.api_catalog import write_api_catalog
from decoder.synthesis.executive import ExecutiveAssignment, build_executive_assignment
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class SynthesisArtifacts:
    final_report: Path
    api_catalog: Path
    executive_prompt: str
    executive_output_file: str


def synthesize(
    slug: str,
    report: StaticAnalysisReport,
    output_root: Path,
) -> SynthesisArtifacts:
    """Produce final_report.md + api_catalog.md and the executive prompt."""
    catalog_path = write_api_catalog(report, output_root)

    m = report.metrics
    langs = ", ".join(
        f"{name} ({stats.lines:,} LOC)"
        for name, stats in sorted(m.languages.items(), key=lambda kv: -kv[1].lines)[:5]
    ) or "n/a"
    scale = (
        "## Precomputed scale facts (authoritative: use verbatim)\n\n"
        f"- {m.analyzed_files} analyzed files, {m.total_lines:,} LOC, "
        f"~{m.estimated_tokens:,} tokens\n"
        f"- languages: {langs}"
    )
    executive: ExecutiveAssignment = build_executive_assignment(
        source_path=report.metrics.root, output_root=output_root, scale=scale
    )

    final_path = output_root / "final_report.md"
    final_path.write_text(_build_final(slug, report, output_root), encoding="utf-8")

    logger.info("synthesis: slug=%s final=%s catalog=%s", slug, final_path, catalog_path)

    return SynthesisArtifacts(
        final_report=final_path,
        api_catalog=catalog_path,
        executive_prompt=executive.prompt,
        executive_output_file=executive.output_file,
    )


def _build_final(slug: str, report: StaticAnalysisReport, output_root: Path) -> str:
    lines: list[str] = []
    lines.append(f"# Decoder: {slug}")
    lines.append("")
    lines.append(f"_Assembled {datetime.now(UTC).isoformat(timespec='seconds')}._")
    lines.append("")

    lines.append("## Document Index")
    lines.append("")
    lines.append(
        "- [Executive Summary](executive_summary.md) "
        "_(dispatched by the /decode skill as the final step)_"
    )
    lines.append("- [Architecture](architecture.md)")
    lines.append("- [Domain](domain.md)")
    lines.append("- [Audit](audit.md)")
    lines.append("- [Red Team Review](red_team.md)")
    lines.append("- [QA Report](qa/qa_report.md)")
    lines.append("- [API Catalog](api_catalog.md)")
    lines.append("- [Index (tier, teams, plan)](index.md)")
    lines.append("- Per-file analyses: [modules/](modules/)")
    lines.append("- Raw static report: [static/report.json](static/report.json)")
    lines.append("- Orchestration plan: [plan.json](plan.json)")
    lines.append("- Event log: [events.jsonl](events.jsonl)")
    lines.append("")

    lines.append("## Repository snapshot")
    m = report.metrics
    lines.append(f"- analyzed files: **{m.analyzed_files:,}** (of {m.total_files:,})")
    lines.append(f"- total lines: **{m.total_lines:,}**")
    lines.append(f"- estimated tokens: {m.estimated_tokens:,}")
    if m.commit:
        lines.append(f"- commit: `{m.commit[:12]}`")
    if m.origin_url:
        lines.append(f"- origin: {m.origin_url}")
    lines.append("")
    if m.languages:
        lines.append("### Languages")
        lines.append("")
        lines.append("| Language | Files | Lines |")
        lines.append("|---|---:|---:|")
        for name, stats in sorted(m.languages.items(), key=lambda kv: -kv[1].lines):
            lines.append(f"| {name} | {stats.files:,} | {stats.lines:,} |")
        lines.append("")

    lines.append("## Tier")
    lines.append(f"- tier: **{report.tier.tier.value}**")
    lines.append(f"- {report.tier.reasoning}")
    lines.append("")

    lines.append("## Knowledge footprint")
    lines.append(f"- symbols indexed: {len(report.symbols):,}")
    lines.append(f"- imports indexed: {len(report.imports):,}")
    dep_stats = report.dep_graph_stats
    lines.append(
        f"- dep graph: {dep_stats.get('nodes', 0)} nodes, {dep_stats.get('edges', 0)} edges"
    )
    lines.append("")

    lines.append("## Module deep-dives")
    modules_dir = output_root / "modules"
    if modules_dir.is_dir():
        mds = sorted(modules_dir.glob("*.md"))
        if mds:
            for path in mds:
                lines.append(f"- [`{path.name}`](modules/{path.name})")
        else:
            lines.append("_No module docs generated yet. Run `/decode` to dispatch Team Alpha._")
    else:
        lines.append("_No modules/ directory yet._")
    lines.append("")

    lines.append("## How to read this report")
    lines.append("")
    lines.append(
        "Start with the executive summary for the 30-second briefing. Use the "
        "architecture and domain documents to build your mental model. Consult the "
        "audit and red-team review before trusting any finding as final. Drill into "
        "`modules/` or the API catalog when you need specifics."
    )
    return "\n".join(lines)
