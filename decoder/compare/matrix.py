from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from decoder.compare.alignment import (
    MultiFileAlignment,
    MultiSymbolAlignment,
    align_files_multi,
    align_symbols_multi,
    import_divergence_multi,
    language_breakdown_multi,
)
from decoder.schemas import StaticAnalysisReport


@dataclass(slots=True)
class MatrixArtifacts:
    summary: Path
    feature_matrix: Path
    structure_matrix: Path


def write_matrix_artifacts(
    slugs: list[str],
    reports: dict[str, StaticAnalysisReport],
    output_root: Path,
) -> MatrixArtifacts:
    output_root.mkdir(parents=True, exist_ok=True)

    files = align_files_multi(reports)
    symbols = align_symbols_multi(reports)

    summary_path = output_root / "summary.md"
    summary_path.write_text(
        _render_summary(slugs, reports, files, symbols), encoding="utf-8"
    )

    feature_path = output_root / "feature_matrix.md"
    feature_path.write_text(
        _render_feature_matrix(slugs, symbols), encoding="utf-8"
    )

    structure_path = output_root / "structure_matrix.md"
    structure_path.write_text(
        _render_structure_matrix(slugs, reports, files), encoding="utf-8"
    )

    return MatrixArtifacts(
        summary=summary_path,
        feature_matrix=feature_path,
        structure_matrix=structure_path,
    )


def _render_summary(
    slugs: list[str],
    reports: dict[str, StaticAnalysisReport],
    files: MultiFileAlignment,
    symbols: MultiSymbolAlignment,
) -> str:
    lines: list[str] = []
    repos_str = ", ".join(f"`{s}`" for s in slugs)
    lines.append(f"# Multi-repo comparison: {repos_str}")
    lines.append("")
    lines.append(f"_{len(slugs)} repositórios analisados._")
    lines.append("")

    lines.append("## Size at a glance")
    lines.append("")
    header = "| metric |" + " ".join(f" {s} |" for s in slugs)
    sep = "|---|" + "---:|" * len(slugs)
    lines.append(header)
    lines.append(sep)
    for metric, getter in (
        ("analyzed files", lambda r: f"{r.metrics.analyzed_files:,}"),
        ("total lines", lambda r: f"{r.metrics.total_lines:,}"),
        ("symbols", lambda r: f"{len(r.symbols):,}"),
        ("imports", lambda r: f"{len(r.imports):,}"),
    ):
        row = f"| {metric} |" + " ".join(f" {getter(reports[s])} |" for s in slugs)
        lines.append(row)
    lines.append("")

    lines.append("## Languages (lines per repo)")
    lines.append("")
    lines.append("| language |" + " ".join(f" {s} |" for s in slugs))
    lines.append("|---|" + "---:|" * len(slugs))
    for lang, counts in language_breakdown_multi(reports):
        row = f"| {lang} |" + " ".join(f" {counts.get(s, 0):,} |" for s in slugs)
        lines.append(row)
    lines.append("")

    # File headline
    total_files = len(files.clusters)
    universal_files = sum(1 for c in files.clusters if c.coverage == len(slugs))
    singleton_files = sum(1 for c in files.clusters if c.coverage == 1)
    lines.append("## File alignment headline")
    lines.append(f"- total distinct file paths: {total_files}")
    lines.append(f"- present in ALL {len(slugs)} repos: {universal_files}")
    lines.append(f"- present in only 1 repo: {singleton_files}")
    lines.append(f"- fuzzy-matched pairs (basename similarity): {len(files.fuzzy_pairs)}")
    lines.append("")

    # Symbol headline
    total_symbols = len(symbols.clusters)
    universal_symbols = sum(
        1 for c in symbols.clusters if c.coverage == len(slugs)
    )
    unique_symbols = sum(1 for c in symbols.clusters if c.coverage == 1)
    lines.append("## Symbol alignment headline")
    lines.append(f"- total distinct (kind, name) symbols: {total_symbols}")
    lines.append(f"- shared across ALL repos: {universal_symbols}")
    lines.append(f"- unique to 1 repo: {unique_symbols}")
    lines.append("")

    # Import divergence
    div = import_divergence_multi(reports)
    lines.append("## Import divergence")
    lines.append(f"- imported by ALL repos: {len(div['universal'])}")
    lines.append(f"- majority (>1 but not all): {len(div['majority'])}")
    lines.append(f"- used by only 1 repo: {len(div['unique'])}")
    lines.append("")

    lines.append("## Deeper dives")
    lines.append("- [Feature matrix (symbols)](feature_matrix.md)")
    lines.append("- [Structure matrix (files)](structure_matrix.md)")
    lines.append("- [Consolidated insights prompt](insights_prompt.md) - dispatched by /decode-compare skill")
    lines.append("- [Consolidated insights (agent output)](insights.md) - written by the skill when present")

    return "\n".join(lines)


def _render_feature_matrix(slugs: list[str], symbols: MultiSymbolAlignment) -> str:
    lines: list[str] = []
    lines.append("# Feature matrix")
    lines.append("")
    lines.append(
        "Each row is a `(kind, name)` pair. A checkmark means the repo has a "
        "symbol of that kind and name; `-` means it does not. The `count` column "
        "shows how many repos share the symbol."
    )
    lines.append("")

    header = "| kind | name | count |" + " ".join(f" {s} |" for s in slugs)
    sep = "|---|---|---:|" + "---|" * len(slugs)
    lines.append(header)
    lines.append(sep)

    for cluster in symbols.clusters:
        marks = []
        for slug in slugs:
            sym = cluster.occurrences.get(slug)
            if sym:
                marks.append(f" `{sym.file}:L{sym.start_line}` ")
            else:
                marks.append(" - ")
        lines.append(
            f"| {cluster.kind} | `{cluster.name}` | {cluster.coverage} |"
            + "|".join(marks)
            + "|"
        )

    return "\n".join(lines)


def _render_structure_matrix(
    slugs: list[str],
    reports: dict[str, StaticAnalysisReport],
    files: MultiFileAlignment,
) -> str:
    lines: list[str] = []
    lines.append("# Structure matrix")
    lines.append("")

    # Grouped by coverage descending.
    total = len(slugs)
    groups: dict[int, list] = {}
    for cluster in files.clusters:
        groups.setdefault(cluster.coverage, []).append(cluster)

    for n in sorted(groups, reverse=True):
        header_title = (
            f"Present in ALL {total} repos"
            if n == total
            else f"Present in {n} of {total} repos"
            if n > 1
            else "Present in only 1 repo"
        )
        lines.append(f"## {header_title} ({len(groups[n])} files)")
        lines.append("")
        if n == 1:
            # Include which slug to avoid ambiguity.
            lines.append("| path | repo |")
            lines.append("|---|---|")
            for cluster in groups[n][:200]:
                lines.append(f"| `{cluster.path}` | `{cluster.slugs[0]}` |")
        else:
            lines.append("| path |" + " ".join(f" {s} |" for s in slugs))
            lines.append("|---|" + "---|" * len(slugs))
            for cluster in groups[n][:200]:
                marks = " ".join(
                    " ✓ |" if s in cluster.slugs else " - |" for s in slugs
                )
                lines.append(f"| `{cluster.path}` |{marks}")
        if len(groups[n]) > 200:
            lines.append(f"\n_(+{len(groups[n]) - 200} more in this bucket)_\n")
        lines.append("")

    if files.fuzzy_pairs:
        lines.append("## Fuzzy-matched pairs (basename similarity >= 0.75)")
        lines.append("")
        lines.append("| repo A | path A | repo B | path B | similarity |")
        lines.append("|---|---|---|---|---:|")
        for slug_a, path_a, slug_b, path_b, ratio in sorted(
            files.fuzzy_pairs, key=lambda x: -x[4]
        )[:50]:
            lines.append(
                f"| `{slug_a}` | `{path_a}` | `{slug_b}` | `{path_b}` | {ratio:.2f} |"
            )

    return "\n".join(lines)
