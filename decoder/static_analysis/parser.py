from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tree_sitter import Node, Parser, Query, QueryCursor, Tree
from tree_sitter_language_pack import get_language

from decoder.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ParsedFile:
    path: Path
    language: str
    source: bytes
    tree: Tree


@cache
def _parser(language: str):
    # tree_sitter_language_pack.get_parser() returns a Parser from the pack's
    # vendored native binding whose .parse() rejects bytes, which is
    # incompatible with the tree_sitter 0.25.x API (TypeError: 'bytes' object
    # is not an instance of 'str'). Build the canonical tree_sitter.Parser from
    # the (ABI-compatible) Language instead.
    return Parser(_language(language))


@cache
def _language(language: str):
    return get_language(language)


def parse_file(path: Path, language: str) -> ParsedFile | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("parse_file read failed: %s (%s)", path, exc)
        return None
    if b"\x00" in data[:4096]:
        return None
    parser = _parser(language)
    try:
        tree = parser.parse(data)
    except Exception as exc:
        logger.warning("parse_file tree-sitter failed: %s (%s)", path, exc)
        return None
    return ParsedFile(path=path, language=language, source=data, tree=tree)


def run_query(
    language: str, tree: Tree, query_source: str
) -> list[tuple[int, dict[str, list[Node]]]]:
    lang = _language(language)
    query = Query(lang, query_source)
    cursor = QueryCursor(query)
    return list(cursor.matches(tree.root_node))


def node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
