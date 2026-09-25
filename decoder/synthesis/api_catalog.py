from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from decoder.schemas import StaticAnalysisReport, SymbolRecord

_PUBLIC_KINDS = {"function", "method", "class", "struct", "enum", "interface", "trait", "type"}


def write_api_catalog(report: StaticAnalysisReport, output_root: Path) -> Path:
    """Produce a deterministic API catalog grouped by file."""
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / "api_catalog.md"

    by_file: dict[str, list[SymbolRecord]] = defaultdict(list)
    for sym in report.symbols:
        if sym.kind in _PUBLIC_KINDS and not sym.name.startswith("_"):
            by_file[sym.file].append(sym)

    lines: list[str] = []
    lines.append("# API Catalog")
    lines.append("")
    lines.append(
        f"Derived deterministically from the static analysis of "
        f"{report.metrics.analyzed_files} analyzed file(s) with "
        f"{len(report.symbols)} total symbols."
    )
    lines.append("")

    if not by_file:
        lines.append("_No public symbols detected._")
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    for file_path in sorted(by_file):
        symbols = sorted(by_file[file_path], key=lambda s: s.start_line)
        lines.append(f"## `{file_path}`")
        lines.append("")
        lines.append("| kind | name | lines |")
        lines.append("|---|---|---|")
        for sym in symbols:
            name = sym.qualified_name or sym.name
            lines.append(f"| {sym.kind} | `{name}` | L{sym.start_line}-L{sym.end_line} |")
        lines.append("")

    target.write_text("\n".join(lines), encoding="utf-8")
    return target
