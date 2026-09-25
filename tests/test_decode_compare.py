from pathlib import Path

from typer.testing import CliRunner

from decoder.cli import app
from decoder.compare.alignment import (
    align_files_multi,
    align_symbols_multi,
    import_divergence_multi,
    language_breakdown_multi,
)
from decoder.compare.insights import build_insights_assignment
from decoder.compare.matrix import write_matrix_artifacts
from decoder.compare.pipeline import MAX_REPOS, run_decode_compare
from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE_A = Path(__file__).parent / "fixtures" / "sample_repo"
FIXTURE_B = Path(__file__).parent / "fixtures" / "sample_repo_v2"


def _report(fixture: Path):
    src = resolve_source(str(fixture))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def _make_fixture_c(tmp_path: Path) -> Path:
    root = tmp_path / "sample_repo_v3"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "class Calculator:\n"
        "    def __init__(self) -> None:\n"
        "        self.value = 0\n"
        "\n"
        "    def add(self, x: int) -> int:\n"
        "        self.value += x\n"
        "        return self.value\n",
        encoding="utf-8",
    )
    (root / "src" / "rules.py").write_text(
        "def enforce_positive(x: int) -> int:\n"
        "    if x < 0:\n"
        "        raise ValueError('no negatives')\n"
        "    return x\n",
        encoding="utf-8",
    )
    return root


def test_align_files_multi_clusters_by_path() -> None:
    reports = {"a": _report(FIXTURE_A), "b": _report(FIXTURE_B)}
    alignment = align_files_multi(reports)
    clusters_by_path = {c.path: c for c in alignment.clusters}
    assert "src/main.py" in clusters_by_path
    assert clusters_by_path["src/main.py"].coverage == 2
    # sample_repo has src/app.ts, v2 does not
    assert "src/app.ts" in clusters_by_path
    assert clusters_by_path["src/app.ts"].slugs == ["a"]


def test_align_symbols_multi_clusters() -> None:
    reports = {"a": _report(FIXTURE_A), "b": _report(FIXTURE_B)}
    alignment = align_symbols_multi(reports)
    by_name = {(c.kind, c.name): c for c in alignment.clusters}
    assert by_name[("class", "Calculator")].coverage == 2
    # multiply only in a
    assert by_name[("function", "multiply")].slugs == ["a"]
    # subtract only in b
    assert by_name[("function", "subtract")].slugs == ["b"]


def test_language_breakdown_multi_includes_all_repos() -> None:
    reports = {"a": _report(FIXTURE_A), "b": _report(FIXTURE_B)}
    rows = language_breakdown_multi(reports)
    langs = {lang: counts for lang, counts in rows}
    assert langs["python"]["a"] > 0 and langs["python"]["b"] > 0
    assert langs["typescript"]["a"] > 0
    assert langs["typescript"]["b"] == 0


def test_import_divergence_multi_buckets() -> None:
    reports = {"a": _report(FIXTURE_A), "b": _report(FIXTURE_B)}
    div = import_divergence_multi(reports)
    # src.user is imported by both -> universal
    assert "src.user" in div["universal"]
    # ./utils/format only imported by a
    assert "./utils/format" in div["unique"]


def test_write_matrix_artifacts_produces_three_files(tmp_path: Path) -> None:
    reports = {"a": _report(FIXTURE_A), "b": _report(FIXTURE_B)}
    artifacts = write_matrix_artifacts(["a", "b"], reports, tmp_path / "out")
    assert artifacts.summary.exists()
    assert artifacts.feature_matrix.exists()
    assert artifacts.structure_matrix.exists()
    feature = artifacts.feature_matrix.read_text()
    assert "Calculator" in feature
    assert "| kind | name |" in feature


def test_insights_prompt_references_decode_and_compare_roots(tmp_path: Path) -> None:
    assignment = build_insights_assignment(
        slugs=["a", "b", "c"],
        compare_root=tmp_path / "compare",
        decode_root=tmp_path / "decode",
    )
    assert "`a`, `b`, `c`" in assignment.prompt
    assert str(tmp_path / "compare") in assignment.prompt
    assert str(tmp_path / "decode") in assignment.prompt
    assert assignment.output_file.endswith("insights.md")


def test_run_decode_compare_two_repos(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_decode_compare([str(FIXTURE_A), str(FIXTURE_B)])
    assert result.slugs == [FIXTURE_A.name, FIXTURE_B.name]
    assert result.matrix.summary.exists()
    assert result.insights_prompt_file.exists()
    # Both sides persisted their static report.
    for side in result.sides:
        assert (side.out_dir / "static" / "report.json").exists()


def test_run_decode_compare_three_repos(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    fixture_c = _make_fixture_c(tmp_path)
    result = run_decode_compare([str(FIXTURE_A), str(FIXTURE_B), str(fixture_c)])
    assert len(result.slugs) == 3
    assert result.matrix.summary.exists()
    summary = result.matrix.summary.read_text()
    # Header should mention all three slugs.
    for slug in result.slugs:
        assert f"`{slug}`" in summary


def test_run_decode_compare_rejects_too_many(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    import pytest

    with pytest.raises(ValueError, match="2 to"):
        run_decode_compare(
            [str(FIXTURE_A)] * (MAX_REPOS + 1),
            include_history=False,
        )


def test_run_decode_compare_rejects_single(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    import pytest

    with pytest.raises(ValueError, match="2 to"):
        run_decode_compare([str(FIXTURE_A)], include_history=False)


def test_cli_decode_compare_produces_outputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["decode-compare", str(FIXTURE_A), str(FIXTURE_B)]
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    joined = f"{FIXTURE_A.name}__vs__{FIXTURE_B.name}"
    out = tmp_path / "docs" / "compare" / joined
    assert (out / "summary.md").exists()
    assert (out / "feature_matrix.md").exists()
    assert (out / "structure_matrix.md").exists()
    assert (out / "insights_prompt.md").exists()


def test_cli_decode_compare_insights_prompt_is_clean(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "decode-compare",
            "--insights-prompt",
            str(FIXTURE_A),
            str(FIXTURE_B),
        ],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "Consolidated Insights" in result.stdout
    assert "feature_matrix.md" in result.stdout


def test_cli_decode_compare_caps_at_four(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["decode-compare", *([str(FIXTURE_A)] * 5)],
    )
    assert result.exit_code != 0
