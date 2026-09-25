from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from decoder.schemas import SymbolKind, SymbolRecord
from decoder.static_analysis.languages import ANALYZED_LANGUAGES
from decoder.static_analysis.parser import ParsedFile, node_text, parse_file, run_query
from decoder.static_analysis.queries import KIND_MAP, SYMBOL_QUERIES
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


def extract_symbols(path: Path, relative: str, language: str) -> list[SymbolRecord]:
    if language not in ANALYZED_LANGUAGES:
        return []
    query = SYMBOL_QUERIES.get(language)
    if not query:
        return []
    parsed = parse_file(path, language)
    if not parsed:
        return []
    return _extract(parsed, relative, query)


def _extract(parsed: ParsedFile, relative: str, query: str) -> list[SymbolRecord]:
    symbols: list[SymbolRecord] = []
    try:
        matches = run_query(parsed.language, parsed.tree, query)
    except Exception as exc:
        logger.warning("query failed on %s: %s", parsed.path, exc)
        return []

    for _pattern_index, captures in matches:
        kind_prefix = _detect_kind(captures)
        if not kind_prefix:
            continue

        name_nodes = captures.get(f"{kind_prefix}.name", [])
        body_nodes = captures.get(f"{kind_prefix}.body", [])
        if not name_nodes or not body_nodes:
            continue

        name_node = name_nodes[0]
        body_node = body_nodes[0]

        parent = _parent_symbol(body_node, parsed.source)
        kind: SymbolKind = KIND_MAP[kind_prefix]  # type: ignore[assignment]

        # Auto-upgrade function to method when enclosed by a class.
        if kind == "function" and parent:
            kind = "method"

        signature = doc = None
        if kind in ("function", "method"):
            signature = _extract_signature(body_node, parsed.source)
            doc = _extract_docstring(body_node, parsed.source)

        symbols.append(
            SymbolRecord(
                file=relative,
                language=parsed.language,
                kind=kind,
                name=node_text(name_node, parsed.source),
                qualified_name=f"{parent}.{node_text(name_node, parsed.source)}"
                if parent
                else None,
                start_line=body_node.start_point[0] + 1,
                end_line=body_node.end_point[0] + 1,
                parent=parent,
                signature=signature,
                docstring=doc,
            )
        )
    return symbols


def _extract_signature(node: Node, source: bytes) -> str | None:
    """The parameter list of a callable (e.g. '(self, x: int)'), if the grammar
    exposes a `parameters` field: works for Python, JS/TS, Go, Rust."""
    params = node.child_by_field_name("parameters")
    if params is None:
        return None
    text = node_text(params, source).strip()
    return text[:200] or None


def _extract_docstring(node: Node, source: bytes) -> str | None:
    """First string literal in a Python function body (the docstring), if any."""
    body = node.child_by_field_name("body")
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    # grammar may give the docstring as a bare `string` or wrapped in an
    # `expression_statement`: handle both; only the FIRST statement counts.
    if first.type == "expression_statement" and first.named_children:
        first = first.named_children[0]
    if first.type != "string":
        return None
    text = node_text(first, source).strip().strip('"').strip("'").strip()
    return " ".join(text.split())[:200] or None


def _detect_kind(captures: dict[str, list[Node]]) -> str | None:
    for capture_name in captures:
        prefix, _, suffix = capture_name.partition(".")
        if suffix == "body" and prefix in KIND_MAP:
            return prefix
    return None


_CLASS_NODE_TYPES: set[str] = {
    "class_definition",  # python
    "class_declaration",  # js/ts/java/kotlin
    "interface_declaration",  # java
    "object_declaration",  # kotlin
    "impl_item",  # rust
}

_NAME_NODE_TYPES = {"identifier", "type_identifier", "simple_identifier"}


def _parent_symbol(node: Node, source: bytes) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in _CLASS_NODE_TYPES:
            name_node = current.child_by_field_name("name")
            if name_node is None:
                # Kotlin class/object expose no "name" field: find the identifier child.
                name_node = next(
                    (c for c in current.named_children if c.type in _NAME_NODE_TYPES),
                    None,
                )
            return node_text(name_node, source) if name_node is not None else None
        current = current.parent
    return None
