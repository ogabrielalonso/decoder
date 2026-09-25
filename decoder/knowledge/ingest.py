from __future__ import annotations

from dataclasses import dataclass

from decoder.knowledge.graph_store import GraphStore
from decoder.knowledge.vector_store import VectorStore
from decoder.schemas import StaticAnalysisReport
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class IngestSummary:
    slug: str
    nodes_written: int
    edges_written: int
    vectors_written: int


def ingest_static_report(
    slug: str,
    report: StaticAnalysisReport,
    graph_store: GraphStore,
    vector_store: VectorStore | None = None,
    *,
    reset: bool = True,
) -> IngestSummary:
    if reset:
        graph_store.clear_slug(slug)

    files_seen: set[str] = set()
    for sym in report.symbols:
        files_seen.add(sym.file)
    for edge in report.imports:
        files_seen.add(edge.source_file)

    for path in sorted(files_seen):
        graph_store.add_file(slug, path, language=_lang_for_file(path, report))

    graph_store.add_symbols(slug, report.symbols)
    graph_store.add_imports(slug, report.imports)

    vectors = 0
    if vector_store is not None:
        vectors = vector_store.add_symbols(slug, report.symbols)

    counts = graph_store.counts(slug)
    logger.info(
        "ingest: slug=%s nodes=%d edges=%d vectors=%d",
        slug,
        counts["nodes"],
        counts["edges"],
        vectors,
    )
    return IngestSummary(
        slug=slug,
        nodes_written=counts["nodes"],
        edges_written=counts["edges"],
        vectors_written=vectors,
    )


def _lang_for_file(path: str, report: StaticAnalysisReport) -> str | None:
    for sym in report.symbols:
        if sym.file == path:
            return sym.language
    return None
