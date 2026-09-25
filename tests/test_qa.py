from pathlib import Path

from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.qa.coverage import compute_coverage
from decoder.qa.red_team import build_red_team_assignment
from decoder.qa.runner import run_qa, write_qa_artifacts
from decoder.qa.validator import validate_module_docs, validate_team_outputs
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"

GOOD_MODULE_DOC = """## `src/main.py`

### Purpose
Entry point that wires `Calculator` with `User` and exercises both.

### Key symbols
- `Calculator` (class, L5-L15): holds running state and exposes add/scale.
- `run` (function, L18-L23): builds a Calculator and a User then prints.

### External dependencies
- `src.utils.math_tools` (from_import)
- `src.user` (from_import)

### Notes
- Tightly couples Calculator to the math_tools helpers.
"""

POOR_MODULE_DOC = """## `src/main.py`

### Purpose
does stuff.
"""


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def _seed_output_root(tmp_path: Path, doc: str = GOOD_MODULE_DOC) -> Path:
    out = tmp_path / "decode-out"
    (out / "modules").mkdir(parents=True)
    (out / "modules" / "alpha-worker-00.md").write_text(doc, encoding="utf-8")
    (out / "architecture.md").write_text(
        "# Architecture\n\n## Overview\nUses `Calculator` (L5-L15).",
        encoding="utf-8",
    )
    (out / "domain.md").write_text(
        "# Domain\n\n## Entities\n- **Calculator**: accumulator (`src/main.py:L5-L15`)",
        encoding="utf-8",
    )
    (out / "audit.md").write_text(
        "# Audit\n\n## Security findings\n- low: `src/main.py:L18-L23`",
        encoding="utf-8",
    )
    (out / "static").mkdir()
    return out


def test_coverage_reports_high_when_documents_mention_symbols(tmp_path: Path) -> None:
    out = _seed_output_root(tmp_path)
    report = _make_report()
    cov = compute_coverage(report, out)
    assert cov.symbol_coverage >= 0.3
    assert cov.file_coverage > 0.1


def test_validator_flags_missing_sections(tmp_path: Path) -> None:
    out = _seed_output_root(tmp_path, doc=POOR_MODULE_DOC)
    result = validate_module_docs(out)
    assert result.documents_checked == 1
    messages = {i.message for i in result.issues}
    assert any("required section missing" in m for m in messages)


def test_validator_accepts_good_module_doc(tmp_path: Path) -> None:
    out = _seed_output_root(tmp_path, doc=GOOD_MODULE_DOC)
    result = validate_module_docs(out)
    error_issues = [i for i in result.issues if i.severity == "error"]
    assert not error_issues, [i.message for i in error_issues]


def test_validate_team_outputs_all_present(tmp_path: Path) -> None:
    out = _seed_output_root(tmp_path)
    result = validate_team_outputs(out)
    assert result.documents_checked == 3
    assert not any(i.severity == "error" for i in result.issues)


def test_validate_team_outputs_flags_missing(tmp_path: Path) -> None:
    out = tmp_path / "empty"
    out.mkdir()
    result = validate_team_outputs(out)
    errors = [i for i in result.issues if i.severity == "error"]
    assert len(errors) == 3


def test_red_team_prompt_references_all_outputs(tmp_path: Path) -> None:
    out = tmp_path / "rt"
    out.mkdir()
    assignment = build_red_team_assignment(
        source_path=tmp_path / "src",
        output_root=out,
        static_report_path=tmp_path / "report.json",
    )
    for needle in (
        "modules/",
        "architecture.md",
        "domain.md",
        "audit.md",
        "report.json",
    ):
        assert needle in assignment.prompt
    assert assignment.output_file.endswith("red_team.md")


def test_run_qa_end_to_end(tmp_path: Path) -> None:
    out = _seed_output_root(tmp_path)
    report = _make_report()
    qa = run_qa("sample", report, out)
    artifacts = write_qa_artifacts(qa, out)
    assert artifacts["json"].exists()
    assert artifacts["markdown"].exists()
    data = artifacts["json"].read_text(encoding="utf-8")
    assert "coverage" in data
    assert "red_team" in data
