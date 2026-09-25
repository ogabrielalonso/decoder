from __future__ import annotations

from pathlib import Path

import networkx as nx

from decoder.schemas import ImportEdge, ImportKind
from decoder.static_analysis.languages import ANALYZED_LANGUAGES
from decoder.static_analysis.parser import node_text, parse_file, run_query
from decoder.static_analysis.queries import IMPORT_QUERIES
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


_KIND_BY_CAPTURE: dict[str, ImportKind] = {
    "import.node": "import",
    "from_import.node": "from_import",
    "use.node": "use",
}


def extract_imports(path: Path, relative: str, language: str) -> list[ImportEdge]:
    if language not in ANALYZED_LANGUAGES:
        return []
    query = IMPORT_QUERIES.get(language)
    if not query:
        return []
    parsed = parse_file(path, language)
    if not parsed:
        return []

    edges: list[ImportEdge] = []
    try:
        matches = run_query(language, parsed.tree, query)
    except Exception as exc:
        logger.warning("import query failed on %s: %s", path, exc)
        return []

    for _pattern, captures in matches:
        kind = _select_kind(captures)
        module = _select_module(captures, parsed.source)
        node = _select_node(captures)
        if not module:
            continue

        edges.append(
            ImportEdge(
                source_file=relative,
                target=_normalize_module(module),
                kind=kind,
                line=(node.start_point[0] + 1) if node is not None else None,
            )
        )
    return edges


def _select_kind(captures: dict[str, list]) -> ImportKind:
    for capture_name, kind in _KIND_BY_CAPTURE.items():
        if capture_name in captures:
            return kind
    return "import"


def _select_module(captures: dict[str, list], source: bytes) -> str | None:
    for capture_name, nodes in captures.items():
        if capture_name.endswith(".module") and nodes:
            return node_text(nodes[0], source)
    # Fallback: for `use.node` (Rust), extract the text of the use declaration itself.
    use_nodes = captures.get("use.node") or []
    if use_nodes:
        raw = node_text(use_nodes[0], source).strip()
        if raw.startswith("use "):
            raw = raw[4:]
        return raw.rstrip(";").strip()
    return None


def _select_node(captures: dict[str, list]):
    for capture_name, nodes in captures.items():
        if capture_name.endswith(".node") and nodes:
            return nodes[0]
    return None


def _normalize_module(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


_SOURCE_EXTS = (
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs",
)


def _noext(p: str) -> str:
    for ext in _SOURCE_EXTS:
        if p.endswith(ext):
            return p[: -len(ext)]
    return p


def resolve_imports(edges: list[ImportEdge], known_files: set[str]) -> None:
    """Populate edge.resolved_file for intra-repo imports (fixes fan-in).

    Without this every fan_in in the coupling table is zero (coupling_table only
    counts edges that have a resolved_file). Resolution is pure path arithmetic:
    relative specifiers resolve against the source dir; dotted module names map to
    path segments; index/__init__ conventions are handled. Third-party / stdlib
    imports find no match and correctly stay None.
    """
    import posixpath

    index: dict[str, str] = {}
    for f in known_files:
        ne = _noext(f)
        index.setdefault(ne, f)
        if ne.endswith("/index"):
            index.setdefault(ne[: -len("/index")], f)
        if ne.endswith("/__init__"):
            index.setdefault(ne[: -len("/__init__")], f)

    for edge in edges:
        target = edge.target
        keys: list[str] = []
        if target.startswith("."):
            src_dir = posixpath.dirname(edge.source_file)
            keys.append(posixpath.normpath(posixpath.join(src_dir, target)))
        else:
            keys.append(target.replace(".", "/"))
            keys.append(target)
        for key in keys:
            hit = index.get(_noext(key.strip("/")))
            if hit:
                edge.resolved_file = hit
                break


def build_graph(edges: list[ImportEdge]) -> nx.DiGraph:
    graph: nx.DiGraph = nx.DiGraph()
    for edge in edges:
        graph.add_edge(edge.source_file, edge.target, kind=edge.kind)
    return graph


def graph_stats(graph: nx.DiGraph) -> dict[str, int]:
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "sources": sum(1 for n in graph.nodes() if graph.out_degree(n) > 0),
        "targets": sum(1 for n in graph.nodes() if graph.in_degree(n) > 0),
    }
