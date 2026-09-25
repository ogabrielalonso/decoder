from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from decoder.knowledge.embeddings import Embedder
from decoder.schemas import SymbolRecord
from decoder.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class VectorHit:
    id: str
    score: float
    text: str
    metadata: dict[str, str]


class VectorStore:
    """Thin wrapper over a persistent ChromaDB collection.

    The collection has three logical namespaces, separated by the metadata
    field `kind`: "symbol", "file_summary", "module_doc".
    """

    def __init__(
        self,
        persist_dir: Path,
        embedder: Embedder,
        *,
        collection: str = "decoder",
    ) -> None:
        import chromadb

        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(name=collection)
        self._embedder = embedder

    def count(self) -> int:
        return self._collection.count()

    def add_symbols(self, slug: str, symbols: list[SymbolRecord]) -> int:
        if not symbols:
            return 0
        ids: list[str] = []
        docs: list[str] = []
        metadatas: list[dict[str, str]] = []
        for sym in symbols:
            sid = f"{slug}::sym::{sym.file}::{sym.name}::{sym.start_line}"
            text = _symbol_text(sym)
            ids.append(sid)
            docs.append(text)
            metadatas.append(
                {
                    "slug": slug,
                    "kind": "symbol",
                    "symbol_kind": sym.kind,
                    "file": sym.file,
                    "name": sym.name,
                    "language": sym.language,
                    "start_line": str(sym.start_line),
                    "end_line": str(sym.end_line),
                }
            )
        embeddings = self._embedder.embed(docs)
        # chromadb's stub union (NDArray | Sequence[float] | Mapping[str, ...]) is
        # runtime-compatible with plain lists/dicts but not structurally, since
        # List/Dict are invariant; cast rather than weaken the types above.
        self._collection.upsert(
            ids=ids,
            documents=docs,
            metadatas=cast(Any, metadatas),
            embeddings=cast(Any, embeddings),
        )
        logger.info("vector_store: upserted %d symbols for slug=%s", len(ids), slug)
        return len(ids)

    def add_module_doc(self, slug: str, worker_id: str, content: str) -> None:
        mid = f"{slug}::module::{worker_id}"
        embeddings = self._embedder.embed([content])
        self._collection.upsert(
            ids=[mid],
            documents=[content],
            metadatas=cast(Any, [{"slug": slug, "kind": "module_doc", "worker_id": worker_id}]),
            embeddings=cast(Any, embeddings),
        )

    def search(
        self,
        query: str,
        *,
        slug: str | None = None,
        kinds: list[str] | None = None,
        k: int = 5,
    ) -> list[VectorHit]:
        if self._collection.count() == 0:
            return []
        embedding = self._embedder.embed([query])[0]
        where: dict | None = None
        filters: list[dict] = []
        if slug:
            filters.append({"slug": slug})
        if kinds:
            filters.append({"kind": {"$in": kinds}})
        if len(filters) == 1:
            where = filters[0]
        elif len(filters) > 1:
            where = {"$and": filters}

        result = self._collection.query(
            query_embeddings=cast(Any, [embedding]),
            n_results=k,
            where=where,
        )
        hits: list[VectorHit] = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        for i, _id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            hits.append(
                VectorHit(
                    id=_id,
                    score=1.0 - float(dists[i]) if dists else 0.0,
                    text=docs[i] if i < len(docs) else "",
                    metadata={key: str(val) for key, val in meta.items()},
                )
            )
        return hits


def _symbol_text(sym: SymbolRecord) -> str:
    parent = f" (in {sym.parent})" if sym.parent else ""
    docstring = f"\n{sym.docstring}" if sym.docstring else ""
    return f"{sym.kind} {sym.name}{parent} from {sym.file}:{sym.start_line}-{sym.end_line}{docstring}"
