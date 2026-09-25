from __future__ import annotations

from pathlib import Path

from decoder.config import Tier
from decoder.schemas import (
    ImportEdge,
    RepoMetrics,
    StaticAnalysisReport,
    SymbolRecord,
    TierDecision,
)
from decoder.static_analysis.graph_metrics import (
    coupling_table,
    dependency_health,
    god_modules,
    long_functions,
    pattern_vocabulary,
    render_entity_candidates,
    render_glossary_candidates,
    render_metrics_block,
    top_hubs,
)


def _report_with_symbols(symbols):
    return StaticAnalysisReport(
        metrics=RepoMetrics(root=Path("/x")),
        tier=TierDecision(tier=Tier.SMALL, reasoning="t", teams=1, workers_per_team=3),
        symbols=symbols,
    )


def _sym(name, kind, start, end, file="a.py"):
    return SymbolRecord(
        file=file, language="python", kind=kind, name=name, start_line=start, end_line=end
    )


def test_long_functions_flags_only_big_callables() -> None:
    r = _report_with_symbols([
        _sym("big", "function", 1, 60),      # 60 lines -> flagged
        _sym("small", "function", 70, 75),   # 6 lines -> not
        _sym("HugeClass", "class", 1, 300),  # not a callable -> ignored
    ])
    names = {f["name"] for f in long_functions(r, min_lines=50)}
    assert names == {"big"}


def test_render_entity_candidates_has_full_line_range() -> None:
    r = _report_with_symbols([_sym("Order", "class", 3, 20)])
    out = render_entity_candidates(r)
    assert "`Order`" in out and "L3-L20" in out


def _report(imports=None, sym_counts=None):
    return StaticAnalysisReport(
        metrics=RepoMetrics(root=Path("/x")),
        tier=TierDecision(tier=Tier.SMALL, reasoning="t", teams=1, workers_per_team=3),
        imports=imports or [],
        symbols_by_file_count=sym_counts or {},
    )


def _imp(src, target, resolved=None):
    return ImportEdge(source_file=src, target=target, kind="import", resolved_file=resolved)


def test_churn_hotspots_ranks_by_commits() -> None:
    from decoder.schemas import FileHistory
    from decoder.static_analysis.graph_metrics import churn_hotspots

    r = StaticAnalysisReport(
        metrics=RepoMetrics(root=Path("/x")),
        tier=TierDecision(tier=Tier.SMALL, reasoning="t", teams=1, workers_per_team=3),
        history=[
            FileHistory(file="hot.py", commits=20, insertions=100, deletions=50, authors=["a", "b"]),
            FileHistory(file="cold.py", commits=1, insertions=2, deletions=0, authors=["a"]),
        ],
    )
    top = churn_hotspots(r)
    assert top[0]["file"] == "hot.py" and top[0]["churn"] == 150 and top[0]["authors"] == 2


def test_top_hubs_ranks_by_fan_in() -> None:
    edges = [_imp("a.py", "h", resolved="hub.py"), _imp("b.py", "h", resolved="hub.py")]
    hubs = top_hubs(_report(imports=edges))
    assert hubs and hubs[0]["file"] == "hub.py" and hubs[0]["fan_in"] == 2


def test_pattern_vocabulary_detects_service_layer() -> None:
    r = _report(sym_counts={"src/services/auth_service.py": 3, "src/repository/user.py": 2})
    pats = {p["pattern"] for p in pattern_vocabulary(r)}
    assert "service layer" in pats and "DDD" in pats


def test_glossary_candidates_lists_entities() -> None:
    r = _report_with_symbols([_sym("Order", "class", 1, 10), _sym("Customer", "struct", 12, 20)])
    out = render_glossary_candidates(r)
    assert "`Order`" in out and "`Customer`" in out


def test_circular_deps_finds_scc() -> None:
    from decoder.static_analysis.graph_metrics import circular_deps

    edges = [_imp("a.py", "b", resolved="b.py"), _imp("b.py", "a", resolved="a.py")]
    cycles = circular_deps(_report(imports=edges))
    assert any(set(c) == {"a.py", "b.py"} for c in cycles)


def test_resolve_imports_populates_resolved_file_and_fan_in() -> None:
    from decoder.static_analysis.dep_graph import resolve_imports

    known = {"src/app.ts", "src/utils/format.ts", "decoder/schemas.py", "pkg/__init__.py"}
    edges = [
        _imp("src/app.ts", "./utils/format"),  # relative JS -> src/utils/format.ts
        _imp("x.py", "decoder.schemas"),  # python dotted -> decoder/schemas.py
        _imp("y.py", "pkg"),  # package -> pkg/__init__.py
        _imp("z.py", "react"),  # third-party -> stays None
    ]
    resolve_imports(edges, known)
    assert edges[0].resolved_file == "src/utils/format.ts"
    assert edges[1].resolved_file == "decoder/schemas.py"
    assert edges[2].resolved_file == "pkg/__init__.py"
    assert edges[3].resolved_file is None
    # fan-in is now non-zero (the bug: it was always 0 before resolution)
    rows = {r["file"]: r for r in coupling_table(_report(imports=edges))}
    assert rows["src/utils/format.ts"]["fan_in"] == 1


def test_coupling_table_counts_fan_in_out() -> None:
    report = _report(imports=[
        _imp("a.py", "b", resolved="b.py"),
        _imp("a.py", "c", resolved="c.py"),
        _imp("d.py", "b", resolved="b.py"),
    ])
    rows = {r["file"]: r for r in coupling_table(report)}
    assert rows["a.py"]["fan_out"] == 2 and rows["a.py"]["fan_in"] == 0
    assert rows["b.py"]["fan_in"] == 2  # imported by a and d
    # most-coupled first
    assert coupling_table(report)[0]["total"] >= coupling_table(report)[-1]["total"]


def test_god_modules_flags_large_or_coupled() -> None:
    report = _report(
        imports=[_imp(f"x{i}.py", "hub", resolved="hub.py") for i in range(20)],
        sym_counts={"big.py": 40, "hub.py": 2, "small.py": 3},
    )
    gods = {g["file"] for g in god_modules(report, symbol_threshold=25, coupling_threshold=15)}
    assert "big.py" in gods  # 40 symbols
    assert "hub.py" in gods  # fan_in 20
    assert "small.py" not in gods


def test_dependency_health_pyproject_and_package_json(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2.0", "flask"]\n'
    )
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"react": "^18.0.0", "left-pad": "1.3.0"}}'
    )
    deps = dependency_health(tmp_path)
    by_name = {d["name"]: d for d in deps}
    assert by_name["requests"]["pinned"] is True  # has >=
    assert by_name["flask"]["pinned"] is False  # bare
    assert by_name["react"]["pinned"] is False  # ^range
    assert by_name["left-pad"]["pinned"] is True  # exact x.y.z


def test_render_metrics_block_has_sections(tmp_path: Path) -> None:
    report = _report(
        imports=[_imp("a.py", "b", resolved="b.py")],
        sym_counts={"a.py": 40},
    )
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["flask"]\n')
    block = render_metrics_block(report, tmp_path)
    assert "Coupling" in block
    assert "god modules" in block.lower()
    assert "Dependency health" in block
    assert "UNPINNED `flask`" in block
