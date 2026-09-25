from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from decoder.schemas import StaticAnalysisReport, SymbolRecord


@dataclass(slots=True)
class CoverageGap:
    kind: str
    identifier: str
    reason: str


@dataclass(slots=True)
class WeakModule:
    """A module doc where symbols are listed but mostly NOT described: a concentrated
    depth hole an overall average would hide. Drives targeted re-dispatch."""

    module: str  # e.g. "alpha-worker-17"
    described: int
    rendered: int


@dataclass(slots=True)
class CoverageReport:
    total_files: int
    files_mentioned: int
    total_symbols: int
    symbols_mentioned: int
    # DESCRIPTION (not just listing): of the symbols rendered in the module docs,
    # how many carry a per-symbol prose description vs. only their name/type/line.
    # `symbol_coverage` above only proves a symbol was MENTIONED; this proves DEPTH.
    symbols_rendered: int = 0
    symbols_described: int = 0
    # Per-module depth holes (described/rendered below threshold): surfaced so the
    # skill can re-dispatch exactly those workers instead of trusting the average.
    weak_modules: list[WeakModule] = field(default_factory=list)
    gaps: list[CoverageGap] = field(default_factory=list)

    @property
    def file_coverage(self) -> float:
        return self.files_mentioned / self.total_files if self.total_files else 1.0

    @property
    def symbol_coverage(self) -> float:
        return self.symbols_mentioned / self.total_symbols if self.total_symbols else 1.0

    @property
    def symbol_description_rate(self) -> float:
        return self.symbols_described / self.symbols_rendered if self.symbols_rendered else 1.0


def compute_coverage(
    report: StaticAnalysisReport, output_root: Path
) -> CoverageReport:
    haystack = _collect_haystack(output_root)

    files = {sym.file for sym in report.symbols}
    # Include pure-file imports even when no symbols were extracted.
    for edge in report.imports:
        files.add(edge.source_file)

    files_mentioned = 0
    symbols_mentioned = 0
    gaps: list[CoverageGap] = []

    for f in sorted(files):
        if _mentions(haystack, f) or _mentions(haystack, Path(f).name):
            files_mentioned += 1
        else:
            gaps.append(
                CoverageGap(kind="file", identifier=f, reason="not referenced in any team output")
            )

    for sym in report.symbols:
        if _symbol_mentioned(haystack, sym):
            symbols_mentioned += 1
        else:
            gaps.append(
                CoverageGap(
                    kind="symbol",
                    identifier=f"{sym.file}:{sym.name}",
                    reason="name not referenced in any team output",
                )
            )

    per_module = _symbol_description_stats(output_root / "modules")
    rendered = sum(r for r, _ in per_module.values())
    described = sum(d for _, d in per_module.values())
    # A weak module: enough symbols to matter (>=10) but < 60% described: a
    # concentrated hole the overall average hides. Worst first.
    weak = [
        WeakModule(module=name, described=d, rendered=r)
        for name, (r, d) in per_module.items()
        if r >= 10 and d / r < 0.60
    ]
    weak.sort(key=lambda w: (w.described / w.rendered if w.rendered else 1.0, -w.rendered))

    return CoverageReport(
        total_files=len(files),
        files_mentioned=files_mentioned,
        total_symbols=len(report.symbols),
        symbols_mentioned=symbols_mentioned,
        symbols_rendered=rendered,
        symbols_described=described,
        weak_modules=weak,
        gaps=gaps,
    )


# A rendered Alpha symbol line: "- `name` (type, L12-L34)" optionally "...): desc".
_SYM_LINE = re.compile(r"^- `[^`]+` \([^)]*L\d")
_SYM_DESCRIBED = re.compile(r"^- `[^`]+` \([^)]*\):\s+\S")


def _symbol_description_stats(modules_dir: Path) -> dict[str, tuple[int, int]]:
    """Per module doc, count (symbols rendered, symbols carrying a prose description).

    Distinguishes a symbol that was merely LISTED (name/type/line) from one that was
    actually DESCRIBED. Returned per-module so a concentrated depth hole (one worker at
    0%) is visible instead of being averaged away across the whole repo.
    """
    per_module: dict[str, tuple[int, int]] = {}
    if not modules_dir.is_dir():
        return per_module
    for path in sorted(modules_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rendered = described = 0
        for line in text.splitlines():
            if _SYM_LINE.match(line):
                rendered += 1
                if _SYM_DESCRIBED.match(line):
                    described += 1
        if rendered:
            per_module[path.stem] = (rendered, described)
    return per_module


def _collect_haystack(output_root: Path) -> str:
    parts: list[str] = []
    if not output_root.exists():
        return ""
    for path in output_root.rglob("*.md"):
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _mentions(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return needle in haystack


_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def _symbol_mentioned(haystack: str, sym: SymbolRecord) -> bool:
    # Simple containment with word-boundary check.
    if sym.name not in haystack:
        return False
    # Avoid false positives via substring (e.g., "add" in "address").
    pattern = re.compile(rf"\b{re.escape(sym.name)}\b")
    return bool(pattern.search(haystack))
