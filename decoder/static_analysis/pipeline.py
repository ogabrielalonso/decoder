from __future__ import annotations

from collections import Counter
from pathlib import Path

from decoder.config import settings
from decoder.schemas import RepoMetrics, StaticAnalysisReport, SymbolRecord, TierDecision
from decoder.static_analysis.dep_graph import (
    build_graph,
    extract_imports,
    graph_stats,
    resolve_imports,
)
from decoder.static_analysis.git_history import collect_history
from decoder.static_analysis.languages import detect_language, is_binary
from decoder.static_analysis.symbol_index import extract_symbols
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


def run_static_analysis(
    root: Path,
    metrics: RepoMetrics,
    tier: TierDecision,
    *,
    include_history: bool = True,
) -> StaticAnalysisReport:
    symbols: list[SymbolRecord] = []
    imports = []
    analyzed_rels: set[str] = set()

    for file_path in _iter_source_files(root):
        rel = str(file_path.relative_to(root))
        language = detect_language(file_path.name)
        if not language:
            continue
        analyzed_rels.add(rel)
        symbols.extend(extract_symbols(file_path, rel, language))
        imports.extend(extract_imports(file_path, rel, language))

    resolve_imports(imports, analyzed_rels)  # populate resolved_file -> fan-in works
    graph = build_graph(imports)
    symbols_by_file: Counter[str] = Counter(sym.file for sym in symbols)

    history = collect_history(root) if include_history else []

    logger.info(
        "static analysis: symbols=%d imports=%d graph_nodes=%d files_with_history=%d",
        len(symbols),
        len(imports),
        graph.number_of_nodes(),
        len(history),
    )

    return StaticAnalysisReport(
        metrics=metrics,
        tier=tier,
        symbols=symbols,
        imports=imports,
        history=history,
        symbols_by_file_count=dict(symbols_by_file),
        dep_graph_stats=graph_stats(graph),
    )


def _iter_source_files(root: Path):
    ignore = set(settings.ignore_patterns)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & ignore:
            continue
        if is_binary(path.name):
            continue
        yield path
