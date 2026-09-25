from __future__ import annotations

from pathlib import Path

from decoder.synthesis.context_pack import build_context_pack


def test_context_pack_condenses_modules(tmp_path: Path) -> None:
    mods = tmp_path / "modules"
    mods.mkdir()
    (mods / "a.md").write_text(
        "## `a.py`\n\n### Purpose\nDoes A.\n\n### Key symbols\n"
        "- `f` (function, L1-L5): a long verbose per-symbol description here\n\n"
        "### External dependencies\n- `os` (import)\n\n### Risks\n- **security** (L3): risky\n"
    )
    pack = build_context_pack(mods)
    assert "Does A." in pack  # purpose kept
    assert "`f` (function, L1-L5)" in pack  # symbol name + range kept
    assert "long verbose" not in pack  # verbose description dropped
    assert "External dependencies" not in pack  # deps section dropped
    assert "**security**" in pack  # risks kept
