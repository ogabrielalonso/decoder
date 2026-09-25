from pathlib import Path

from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.knowledge.embeddings import HashEmbedder
from decoder.knowledge.graph_store import GraphStore, file_node_id, symbol_node_id
from decoder.knowledge.ingest import ingest_static_report
from decoder.knowledge.vector_store import VectorStore
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def test_hash_embedder_produces_normalized_vectors() -> None:
    emb = HashEmbedder(dimensions=64)
    vectors = emb.embed(["hello world", "hello decoder"])
    assert len(vectors) == 2
    assert all(len(v) == 64 for v in vectors)
    for v in vectors:
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-6


def test_graph_store_ingests_symbols_and_imports(tmp_path) -> None:
    store = GraphStore(tmp_path / "graph.sqlite")
    report = _make_report()

    summary = ingest_static_report("sample", report, store, vector_store=None)

    assert summary.nodes_written > 0
    assert summary.edges_written > 0

    graph = store.as_networkx("sample")
    # Expect the main.py file node with symbol CONTAINS edges.
    main_file = file_node_id("sample", "src/main.py")
    assert main_file in graph
    kinds = {graph[main_file][succ]["kind"] for succ in graph.successors(main_file)}
    assert "CONTAINS" in kinds


def test_graph_store_clear_slug_scoped(tmp_path) -> None:
    store = GraphStore(tmp_path / "graph.sqlite")
    report = _make_report()
    ingest_static_report("a", report, store, vector_store=None)
    ingest_static_report("b", report, store, vector_store=None)

    assert store.counts("a")["nodes"] > 0
    assert store.counts("b")["nodes"] > 0

    store.clear_slug("a")
    assert store.counts("a") == {"nodes": 0, "edges": 0}
    assert store.counts("b")["nodes"] > 0


def test_vector_store_round_trips_with_hash_embedder(tmp_path) -> None:
    embedder = HashEmbedder(dimensions=128)
    store = VectorStore(tmp_path / "chroma", embedder, collection="test")
    report = _make_report()

    count = store.add_symbols("sample", report.symbols)
    assert count == len(report.symbols)
    assert store.count() == count

    hits = store.search("Calculator", slug="sample", kinds=["symbol"], k=5)
    assert hits, "expected at least one hit"
    names = {h.metadata.get("name") for h in hits}
    assert "Calculator" in names or any("Calculator" in h.text for h in hits)


def test_graph_store_neighbors(tmp_path) -> None:
    store = GraphStore(tmp_path / "graph.sqlite")
    report = _make_report()
    ingest_static_report("sample", report, store, vector_store=None)

    main_file = file_node_id("sample", "src/main.py")
    neighbors = store.neighbors(main_file, depth=1)
    # main.py contains Calculator etc., so neighbors are non-empty.
    assert len(neighbors) > 0

    # Pick a known symbol.
    calculator_syms = [s for s in report.symbols if s.name == "Calculator"]
    assert calculator_syms
    calc_id = symbol_node_id("sample", calculator_syms[0])
    assert calc_id in neighbors
