from pathlib import Path

from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.static_analysis.dep_graph import extract_imports
from decoder.static_analysis.parser import parse_file
from decoder.static_analysis.pipeline import run_static_analysis
from decoder.static_analysis.symbol_index import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def test_parse_file_accepts_bytes_source() -> None:
    # Regression guard: tree-sitter-language-pack >= 1.8.0 ships a vendored
    # native Parser via get_parser() whose .parse() rejects bytes with
    # "TypeError: 'bytes' object is not an instance of 'str'", silently turning
    # every file into a parse failure (0 symbols / 0 imports). parse_file must
    # build the canonical tree_sitter.Parser and return a real tree for bytes.
    for rel, lang in (("src/app.ts", "typescript"), ("src/main.py", "python")):
        parsed = parse_file(FIXTURE / rel, lang)
        assert parsed is not None, f"parse_file returned None for {rel}"
        assert parsed.tree.root_node.child_count > 0, f"empty parse tree for {rel}"
        assert not parsed.tree.root_node.has_error, f"parse errors in {rel}"


def test_extract_symbols_python_captures_class_and_methods() -> None:
    file = FIXTURE / "src" / "main.py"
    symbols = extract_symbols(file, "src/main.py", "python")
    names = {(s.kind, s.name) for s in symbols}

    assert ("class", "Calculator") in names
    assert ("method", "add") in names
    assert ("method", "scale") in names
    assert ("method", "__init__") in names
    assert ("function", "run") in names


def test_extract_symbols_typescript_captures_interface_and_class() -> None:
    file = FIXTURE / "src" / "app.ts"
    symbols = extract_symbols(file, "src/app.ts", "typescript")
    kinds_and_names = {(s.kind, s.name) for s in symbols}

    assert ("interface", "Greeter") in kinds_and_names
    assert ("class", "Hello") in kinds_and_names
    assert ("method", "greet") in kinds_and_names
    assert ("function", "shout") in kinds_and_names


def test_extract_imports_python_finds_modules() -> None:
    file = FIXTURE / "src" / "main.py"
    edges = extract_imports(file, "src/main.py", "python")
    targets = {e.target for e in edges}

    assert "src.utils.math_tools" in targets
    assert "src.user" in targets


def test_extract_imports_typescript_finds_module_paths() -> None:
    file = FIXTURE / "src" / "app.ts"
    edges = extract_imports(file, "src/app.ts", "typescript")
    targets = {e.target for e in edges}

    assert "./utils/format" in targets


def test_run_static_analysis_produces_report() -> None:
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    report = run_static_analysis(src.path, metrics, tier, include_history=False)

    assert report.metrics.analyzed_files == metrics.analyzed_files
    assert len(report.symbols) >= 6
    assert len(report.imports) >= 3
    assert report.dep_graph_stats["edges"] >= 3
    assert report.symbols_by_file_count["src/main.py"] >= 4


def test_cli_decode_static_only_writes_report(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from decoder.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["decode", "--static-only", "--no-history", str(FIXTURE)])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    report_path = tmp_path / "docs" / "decode" / FIXTURE.name / "static" / "report.json"
    assert report_path.exists()
    data = report_path.read_text()
    assert "symbols" in data
    assert "Calculator" in data


def test_extract_signature_and_docstring(tmp_path) -> None:
    from decoder.static_analysis.symbol_index import extract_symbols

    f = tmp_path / "m.py"
    f.write_text(
        'def greet(name: str, times: int = 1) -> str:\n'
        '    """Return a greeting."""\n'
        '    return name * times\n'
    )
    g = next(s for s in extract_symbols(f, "m.py", "python") if s.name == "greet")
    assert g.signature == "(name: str, times: int = 1)"
    assert g.docstring == "Return a greeting."


def test_scan_security_flags_high_signal_patterns(tmp_path) -> None:
    from decoder.static_analysis.security_scan import scan_security

    (tmp_path / "c.py").write_text(
        'API_KEY = "abcdef1234567890"\n'
        "import subprocess\n"
        "subprocess.run(cmd, shell=True)\n"
    )
    pats = {h["pattern"] for h in scan_security(tmp_path)}
    assert "hardcoded-secret" in pats
    assert "shell-injection-risk" in pats


def test_java_symbol_and_import_extraction(tmp_path) -> None:
    from decoder.static_analysis.dep_graph import extract_imports
    from decoder.static_analysis.symbol_index import extract_symbols

    f = tmp_path / "Calc.java"
    f.write_text(
        "package com.x;\nimport java.util.List;\n"
        "public class Calc {\n    public int add(int a, int b) { return a + b; }\n}\n"
    )
    syms = {s.qualified_name or s.name: s for s in extract_symbols(f, "Calc.java", "java")}
    assert "Calc" in syms and syms["Calc"].kind == "class"
    assert "Calc.add" in syms and syms["Calc.add"].kind == "method"
    assert [i.target for i in extract_imports(f, "Calc.java", "java")] == ["java.util.List"]


def test_kotlin_class_extraction(tmp_path) -> None:
    from decoder.static_analysis.symbol_index import extract_symbols

    f = tmp_path / "Calc.kt"
    f.write_text("package com.x\nclass Calc {\n    fun add(a: Int): Int { return a }\n}\n")
    names = {s.qualified_name or s.name for s in extract_symbols(f, "Calc.kt", "kotlin")}
    assert "Calc" in names and "Calc.add" in names
