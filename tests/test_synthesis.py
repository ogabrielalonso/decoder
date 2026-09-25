from pathlib import Path

from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.static_analysis.pipeline import run_static_analysis
from decoder.synthesis.api_catalog import write_api_catalog
from decoder.synthesis.assembler import synthesize
from decoder.synthesis.executive import build_executive_assignment

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def test_api_catalog_lists_public_symbols(tmp_path: Path) -> None:
    report = _make_report()
    path = write_api_catalog(report, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "# API Catalog" in content
    # Public classes and functions should appear; __init__ (private) should NOT.
    assert "Calculator" in content
    assert "formatName" in content
    assert "`__init__`" not in content


def test_api_catalog_handles_empty_report(tmp_path: Path) -> None:
    report = _make_report()
    report.symbols = []
    path = write_api_catalog(report, tmp_path)
    assert "No public symbols detected" in path.read_text(encoding="utf-8")


def test_executive_prompt_references_all_sources(tmp_path: Path) -> None:
    assignment = build_executive_assignment(tmp_path / "src", tmp_path / "out")
    for needle in ("index.md", "architecture.md", "domain.md", "audit.md", "red_team.md"):
        assert needle in assignment.prompt
    assert assignment.output_file.endswith("executive_summary.md")


def test_synthesize_produces_final_report_and_catalog(tmp_path: Path) -> None:
    report = _make_report()
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "alpha-worker-00.md").write_text("## `src/main.py`\n", encoding="utf-8")
    artifacts = synthesize("sample", report, tmp_path)
    assert artifacts.final_report.exists()
    assert artifacts.api_catalog.exists()
    final = artifacts.final_report.read_text(encoding="utf-8")
    assert "Document Index" in final
    assert "modules/alpha-worker-00.md" in final
    assert artifacts.executive_prompt.strip()
