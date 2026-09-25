from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import networkx as nx

from decoder.schemas import ImportEdge, SymbolRecord
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (source, target, kind)
);
CREATE INDEX IF NOT EXISTS idx_nodes_slug ON nodes(slug);
CREATE INDEX IF NOT EXISTS idx_edges_slug ON edges(slug);
"""


class GraphStore:
    """SQLite-backed node/edge store with networkx export.

    Node ids are namespaced: "<slug>::file::<path>", "<slug>::sym::<path>::<name>::<line>".
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def clear_slug(self, slug: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM edges WHERE slug = ?", (slug,))
            conn.execute("DELETE FROM nodes WHERE slug = ?", (slug,))

    def add_file(self, slug: str, path: str, language: str | None = None) -> str:
        node_id = file_node_id(slug, path)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?)",
                (
                    node_id,
                    "file",
                    slug,
                    path,
                    json.dumps({"path": path, "language": language}),
                ),
            )
        return node_id

    def add_symbols(self, slug: str, symbols: list[SymbolRecord]) -> int:
        if not symbols:
            return 0
        rows: list[tuple] = []
        edges: list[tuple] = []
        for sym in symbols:
            node_id = symbol_node_id(slug, sym)
            file_id = file_node_id(slug, sym.file)
            rows.append(
                (
                    node_id,
                    "symbol",
                    slug,
                    sym.qualified_name or sym.name,
                    json.dumps(sym.model_dump(mode="json")),
                )
            )
            edges.append(
                (
                    file_id,
                    node_id,
                    "CONTAINS",
                    slug,
                    json.dumps({"start_line": sym.start_line, "end_line": sym.end_line}),
                )
            )
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO edges VALUES (?, ?, ?, ?, ?)",
                edges,
            )
        return len(rows)

    def add_imports(self, slug: str, imports: list[ImportEdge]) -> int:
        if not imports:
            return 0
        edges: list[tuple] = []
        for edge in imports:
            source_id = file_node_id(slug, edge.source_file)
            target_id = f"{slug}::ext::{edge.target}"
            # Make sure the external module exists as a node.
            edges.append((source_id, target_id, "IMPORTS", slug, json.dumps(edge.model_dump())))
        with self._connect() as conn:
            # Ensure target nodes exist.
            ext_nodes = {
                (f"{slug}::ext::{e.target}", "module", slug, e.target, json.dumps({"target": e.target}))
                for e in imports
            }
            conn.executemany(
                "INSERT OR IGNORE INTO nodes VALUES (?, ?, ?, ?, ?)",
                list(ext_nodes),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO edges VALUES (?, ?, ?, ?, ?)",
                edges,
            )
        return len(edges)

    def counts(self, slug: str | None = None) -> dict[str, int]:
        with self._connect() as conn:
            if slug:
                n = conn.execute("SELECT COUNT(*) FROM nodes WHERE slug = ?", (slug,)).fetchone()[0]
                e = conn.execute("SELECT COUNT(*) FROM edges WHERE slug = ?", (slug,)).fetchone()[0]
            else:
                n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {"nodes": n, "edges": e}

    def as_networkx(self, slug: str | None = None) -> nx.DiGraph:
        graph: nx.DiGraph = nx.DiGraph()
        with self._connect() as conn:
            if slug:
                nodes = conn.execute(
                    "SELECT id, kind, label, data FROM nodes WHERE slug = ?", (slug,)
                ).fetchall()
                edges = conn.execute(
                    "SELECT source, target, kind, data FROM edges WHERE slug = ?", (slug,)
                ).fetchall()
            else:
                nodes = conn.execute("SELECT id, kind, label, data FROM nodes").fetchall()
                edges = conn.execute(
                    "SELECT source, target, kind, data FROM edges"
                ).fetchall()
        for nid, kind, label, data in nodes:
            graph.add_node(nid, kind=kind, label=label, data=json.loads(data))
        for src, tgt, kind, data in edges:
            graph.add_edge(src, tgt, kind=kind, data=json.loads(data))
        return graph

    def neighbors(self, node_id: str, *, depth: int = 1) -> list[str]:
        graph = self.as_networkx()
        if node_id not in graph:
            return []
        seen: set[str] = {node_id}
        frontier: set[str] = {node_id}
        for _ in range(depth):
            nxt: set[str] = set()
            for n in frontier:
                nxt.update(graph.successors(n))
                nxt.update(graph.predecessors(n))
            nxt -= seen
            seen.update(nxt)
            frontier = nxt
            if not frontier:
                break
        return sorted(seen - {node_id})


def file_node_id(slug: str, path: str) -> str:
    return f"{slug}::file::{path}"


def symbol_node_id(slug: str, sym: SymbolRecord) -> str:
    return f"{slug}::sym::{sym.file}::{sym.name}::{sym.start_line}"
