from __future__ import annotations

from decoder.schemas import ImportEdge, SymbolRecord
from decoder.synthesis.module_structure import (
    FileNarrative,
    _extract_symbol_name,
    assemble_module_doc,
    parse_decomposed_alpha,
    render_external_deps,
    render_file_block,
    render_key_symbols,
)


def test_extract_symbol_name_recovers_decorated_keys() -> None:
    # Agents key symbol descriptions inconsistently; assembly must recover the bare
    # name so the description attaches instead of rendering the symbol bare.
    assert _extract_symbol_name("interface `Foo` (L15-L20)") == "Foo"
    assert _extract_symbol_name("function `bar` ({a, b}) (L22-L114)") == "bar"
    assert _extract_symbol_name("type RequestInterceptor") == "RequestInterceptor"
    assert _extract_symbol_name("class Baz") == "Baz"
    assert _extract_symbol_name("IdeationEngine.constructor") == "IdeationEngine.constructor"
    assert _extract_symbol_name("Plain") == "Plain"


def test_parse_decomposed_alpha_indexes_bare_and_raw_keys() -> None:
    # A decorated key must be reachable by the bare name AND its original form.
    parsed = parse_decomposed_alpha(
        '{"a.ts": {"purpose": "p", "symbols": {"type RequestInterceptor": "transforms req"}, '
        '"risks": []}}'
    )
    syms = parsed["a.ts"].symbols
    assert syms["RequestInterceptor"] == "transforms req"
    assert syms["type RequestInterceptor"] == "transforms req"

_CLEAN = '{"a.py": {"purpose": "does a", "symbols": {"f": "runs f"}, "risks": []}}'


def test_assemble_injects_deterministic_risks() -> None:
    from decoder.synthesis.module_structure import assemble_decomposed_doc

    scope = ["big.py", "hub.py"]
    symbols = [_sym("big.py", "huge", kind="function", start=1, end=70)]  # 70-line fn
    imports = [_imp(f"x{i}.py", "hub") for i in range(11)]  # 11 importers of hub
    for e in imports:
        e.resolved_file = "hub.py"
    doc = assemble_decomposed_doc(scope, symbols, imports, {})
    assert "**complexity**" in doc  # big.py:huge exceeds 50 lines (code-owned)
    assert "**tight-coupling**" in doc  # hub.py fan_in=11 (code-owned)


def test_parse_decomposed_clean_json() -> None:
    out = parse_decomposed_alpha(_CLEAN)
    assert out["a.py"].purpose == "does a"
    assert out["a.py"].symbols["f"] == "runs f"


def test_parse_decomposed_fenced() -> None:
    assert parse_decomposed_alpha(f"```json\n{_CLEAN}\n```")["a.py"].purpose == "does a"


def test_parse_decomposed_with_surrounding_prose() -> None:
    # "Extra data" failure mode: model adds text before/after the object.
    text = f"Here is the analysis:\n{_CLEAN}\nLet me know if you need more."
    assert parse_decomposed_alpha(text)["a.py"].symbols["f"] == "runs f"


def test_parse_decomposed_truncated_salvages_complete_entries() -> None:
    # output-limit failure mode: JSON cut off mid-stream after one full entry.
    truncated = (
        '{"a.py": {"purpose": "does a", "symbols": {"f": "runs f"}, "risks": []}, '
        '"b.py": {"purpose": "half writ'
    )
    out = parse_decomposed_alpha(truncated)
    assert "a.py" in out and out["a.py"].purpose == "does a"  # kept the complete one
    assert "b.py" not in out  # dropped the incomplete one, did not raise


def test_parse_decomposed_empty_and_garbage_never_raise() -> None:
    assert parse_decomposed_alpha("") == {}
    assert parse_decomposed_alpha("I could not complete this task.") == {}
    assert parse_decomposed_alpha("[1, 2, 3]") == {}  # JSON, but not an object


def _sym(file: str, name: str, kind: str = "function", start: int = 1, end: int = 5):
    return SymbolRecord(
        file=file, language="python", kind=kind, name=name, start_line=start, end_line=end
    )


def _imp(file: str, target: str, kind: str = "import"):
    return ImportEdge(source_file=file, target=target, kind=kind)


def test_render_key_symbols_uses_qualified_name_and_lines() -> None:
    s = SymbolRecord(
        file="a.py",
        language="python",
        kind="method",
        name="run",
        qualified_name="App.run",
        start_line=10,
        end_line=20,
    )
    out = render_key_symbols([s])
    assert out == "- `App.run` (method, L10-L20)"


def test_render_key_symbols_sorts_by_line() -> None:
    out = render_key_symbols([_sym("a.py", "b", start=30), _sym("a.py", "a", start=5)])
    assert out.index("`a`") < out.index("`b`")


def test_render_key_symbols_empty() -> None:
    assert render_key_symbols([]) == "_(none detected)_"


def test_render_external_deps_dedupes() -> None:
    imports = [_imp("a.py", "os"), _imp("a.py", "os"), _imp("a.py", "sys")]
    out = render_external_deps(imports)
    assert out.count("`os`") == 1
    assert "`sys`" in out


def test_render_external_deps_empty() -> None:
    assert render_external_deps([]) == "_(none detected)_"


def test_render_file_block_has_all_sections_with_placeholder() -> None:
    block = render_file_block("a.py", [_sym("a.py", "f")], [_imp("a.py", "os")])
    assert "## `a.py`" in block
    assert "### Purpose" in block and "_(pending)_" in block
    assert "### Key symbols" in block and "`f`" in block
    assert "### External dependencies" in block and "`os`" in block
    # no Notes section when no narrative
    assert "### Notes" not in block


def test_render_file_block_with_narrative() -> None:
    nar = FileNarrative(purpose="Entry point.", notes=["spawns threads", "no auth"])
    block = render_file_block("a.py", [_sym("a.py", "f")], [], nar)
    assert "Entry point." in block
    assert "### Notes" in block
    assert "- spawns threads" in block and "- no auth" in block


def test_assemble_module_doc_orders_by_scope_and_separates() -> None:
    scope = ["z.py", "a.py"]
    symbols = [_sym("a.py", "fa"), _sym("z.py", "fz")]
    imports = [_imp("z.py", "os")]
    doc = assemble_module_doc(scope, symbols, imports)
    # scope order preserved: z before a
    assert doc.index("## `z.py`") < doc.index("## `a.py`")
    # blocks separated by ---
    assert "\n---\n" in doc
    # deterministic data present
    assert "`fz`" in doc and "`fa`" in doc and "`os`" in doc


def test_assemble_module_doc_merges_narratives() -> None:
    scope = ["a.py"]
    narratives = {"a.py": FileNarrative(purpose="Does X.", notes=["risky"])}
    doc = assemble_module_doc(scope, [_sym("a.py", "f")], [], narratives)
    assert "Does X." in doc
    assert "- risky" in doc
