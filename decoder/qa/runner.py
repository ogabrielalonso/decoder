from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from decoder.qa.coverage import CoverageReport, compute_coverage
from decoder.qa.red_team import RedTeamAssignment, build_red_team_assignment
from decoder.qa.validator import (
    ValidationReport,
    validate_module_docs,
    validate_team_outputs,
)
from decoder.schemas import StaticAnalysisReport
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class QAReport:
    slug: str
    coverage: CoverageReport
    module_validation: ValidationReport
    team_validation: ValidationReport
    red_team_prompt: str
    red_team_output_file: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "coverage": _dataclass_to_jsonable(self.coverage),
            "module_validation": _dataclass_to_jsonable(self.module_validation),
            "team_validation": _dataclass_to_jsonable(self.team_validation),
            "red_team": {
                "prompt": self.red_team_prompt,
                "output_file": self.red_team_output_file,
            },
            "warnings": self.warnings,
        }


def _coverage_summary(coverage: CoverageReport) -> str:
    """Render the deterministic coverage gaps to hand the red-team agent."""
    lines = [
        "## Precomputed coverage gaps (authoritative: do not recompute)",
        "",
        f"- files {coverage.files_mentioned}/{coverage.total_files} "
        f"({coverage.file_coverage:.0%}); symbols {coverage.symbols_mentioned}/"
        f"{coverage.total_symbols} ({coverage.symbol_coverage:.0%})",
    ]
    if coverage.gaps:
        lines.append(f"- {len(coverage.gaps)} uncommented item(s):")
        for gap in coverage.gaps[:30]:
            lines.append(f"  - {gap.kind} `{gap.identifier}`")
        if len(coverage.gaps) > 30:
            lines.append(f"  - … and {len(coverage.gaps) - 30} more")
    return "\n".join(lines)


def run_qa(slug: str, report: StaticAnalysisReport, output_root: Path) -> QAReport:
    coverage = compute_coverage(report, output_root)
    module_validation = validate_module_docs(output_root)
    team_validation = validate_team_outputs(output_root)
    red: RedTeamAssignment = build_red_team_assignment(
        source_path=report.metrics.root,
        output_root=output_root,
        static_report_path=output_root / "static" / "report.json",
        coverage=_coverage_summary(coverage),
    )

    warnings: list[str] = []
    if coverage.file_coverage < 0.9:
        warnings.append(
            f"file coverage at {coverage.file_coverage:.0%} (< 90%)"
        )
    if coverage.symbol_coverage < 0.7:
        warnings.append(
            f"symbol coverage at {coverage.symbol_coverage:.0%} (< 70%)"
        )
    if coverage.symbol_description_rate < 0.85:
        warnings.append(
            f"symbol description rate at {coverage.symbol_description_rate:.0%} "
            f"({coverage.symbols_described}/{coverage.symbols_rendered} described, not just "
            f"listed): symbol-dense files likely have listed-but-undescribed symbols"
        )
    if coverage.weak_modules:
        shown = ", ".join(f"{w.module} {w.described}/{w.rendered}" for w in coverage.weak_modules[:8])
        warnings.append(
            f"{len(coverage.weak_modules)} module(s) with low symbol-description depth "
            f"(<60%): re-dispatch these workers: {shown}"
        )
    if module_validation.issues:
        warnings.append(
            f"{len(module_validation.issues)} module validation issue(s)"
        )
    if team_validation.issues:
        warnings.append(
            f"{len(team_validation.issues)} team output issue(s)"
        )

    qa = QAReport(
        slug=slug,
        coverage=coverage,
        module_validation=module_validation,
        team_validation=team_validation,
        red_team_prompt=red.prompt,
        red_team_output_file=red.output_file,
        warnings=warnings,
    )
    logger.info(
        "qa: slug=%s file_cov=%.2f sym_cov=%.2f issues=%d",
        slug,
        coverage.file_coverage,
        coverage.symbol_coverage,
        len(module_validation.issues) + len(team_validation.issues),
    )
    return qa


def write_qa_artifacts(qa: QAReport, output_root: Path) -> dict[str, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    qa_dir = output_root / "qa"
    qa_dir.mkdir(exist_ok=True)

    json_path = qa_dir / "qa_report.json"
    json_path.write_text(json.dumps(qa.to_dict(), indent=2), encoding="utf-8")

    md_path = qa_dir / "qa_report.md"
    md_path.write_text(_render_qa_markdown(qa), encoding="utf-8")

    return {"json": json_path, "markdown": md_path}


def _render_qa_markdown(qa: QAReport) -> str:
    lines: list[str] = []
    lines.append(f"# QA Report: {qa.slug}")
    lines.append("")
    c = qa.coverage
    lines.append("## Coverage")
    lines.append(f"- files: {c.files_mentioned}/{c.total_files} ({c.file_coverage:.0%})")
    lines.append(f"- symbols (listed): {c.symbols_mentioned}/{c.total_symbols} ({c.symbol_coverage:.0%})")
    lines.append(
        f"- symbols (described): {c.symbols_described}/{c.symbols_rendered} "
        f"({c.symbol_description_rate:.0%})"
    )
    if c.weak_modules:
        lines.append("")
        lines.append(f"### Weak modules: low description depth ({len(c.weak_modules)})")
        for w in c.weak_modules:
            pct = w.described / w.rendered if w.rendered else 1.0
            lines.append(f"- `{w.module}`: {w.described}/{w.rendered} described ({pct:.0%})")
    if c.gaps:
        lines.append("")
        lines.append(f"### Gaps ({len(c.gaps)})")
        for gap in c.gaps[:40]:
            lines.append(f"- **{gap.kind}** `{gap.identifier}` - {gap.reason}")
        if len(c.gaps) > 40:
            lines.append(f"- ... (+{len(c.gaps) - 40} more)")
    lines.append("")

    lines.append("## Module validation")
    lines.append(f"- documents checked: {qa.module_validation.documents_checked}")
    lines.append(f"- issues: {len(qa.module_validation.issues)}")
    for issue in qa.module_validation.issues[:40]:
        loc = f"{issue.file}::{issue.section}" if issue.section else issue.file
        lines.append(f"  - **{issue.severity}** `{loc}` - {issue.message}")
    lines.append("")

    lines.append("## Team output validation")
    lines.append(f"- documents checked: {qa.team_validation.documents_checked}")
    lines.append(f"- issues: {len(qa.team_validation.issues)}")
    for issue in qa.team_validation.issues[:40]:
        lines.append(f"  - **{issue.severity}** `{issue.file}` - {issue.message}")
    lines.append("")

    if qa.warnings:
        lines.append("## Warnings")
        for warning in qa.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Red team")
    lines.append(
        f"The red-team agent prompt is ready. The `/decode` skill can dispatch "
        f"it via Task tool and save output to `{qa.red_team_output_file}`."
    )
    return "\n".join(lines)


def _dataclass_to_jsonable(obj) -> dict:
    data = asdict(obj)
    return data
