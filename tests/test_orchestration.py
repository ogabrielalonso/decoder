import json
from pathlib import Path

from typer.testing import CliRunner

from decoder.cli import app
from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.orchestration.chunker import FileInfo, build_file_infos, chunk_files
from decoder.orchestration.contracts import ORCHESTRATION_VERSION
from decoder.orchestration.master import plan_from_static
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return src, run_static_analysis(src.path, metrics, tier, include_history=False)


def test_chunker_balances_bytes_across_workers() -> None:
    files = [
        FileInfo(relative=f"f{i}.py", bytes=100, language="python", symbols=1) for i in range(10)
    ]
    chunks = chunk_files(files, worker_count=3)
    # Close-before-overshoot keeps every chunk within the per-worker byte target
    # (no chunk blows past the window because of a boundary file). Count is emergent
    # (>= worker_count), every chunk is non-empty, and no file is dropped.
    target = 1000 // 3
    assert chunks and len(chunks) >= 3
    for chunk in chunks:
        assert chunk.files
        assert chunk.total_bytes <= target
    assert sum(c.total_bytes for c in chunks) == 1000


def test_chunker_handles_fewer_files_than_workers() -> None:
    files = [FileInfo(relative="only.py", bytes=42, language="python", symbols=0)]
    chunks = chunk_files(files, worker_count=4)
    assert len(chunks) == 1
    assert chunks[0].files[0].relative == "only.py"


def test_chunker_caps_files_per_chunk() -> None:
    # 50 tiny files: by bytes alone worker_count=2 would pack ~25 per chunk, but the
    # file cap (10) must split so NO chunk exceeds 10 files (output-bound) and NO
    # file is lost.
    files = [
        FileInfo(relative=f"f{i}.py", bytes=10, language="python", symbols=1) for i in range(50)
    ]
    chunks = chunk_files(files, worker_count=2, max_files_per_chunk=10)
    assert chunks
    assert all(len(c.files) <= 10 for c in chunks)
    assert sum(len(c.files) for c in chunks) == 50


def test_chunker_caps_symbols_per_chunk() -> None:
    # 10 files of 50 symbols each (500 total): the symbol cap (100) must split so no
    # chunk exceeds 100 symbols: isolating dense files so the agent describes them all.
    files = [
        FileInfo(relative=f"f{i}.py", bytes=100, language="python", symbols=50) for i in range(10)
    ]
    chunks = chunk_files(files, worker_count=2, max_symbols_per_chunk=100)
    assert chunks
    assert all(sum(fi.symbols for fi in c.files) <= 100 for c in chunks)
    assert sum(len(c.files) for c in chunks) == 10


def test_build_file_infos_includes_fixture_sources() -> None:
    src, report = _make_report()
    infos, _assets = build_file_infos(src.path, report.symbols)
    paths = {info.relative for info in infos}
    assert "src/main.py" in paths
    assert "src/app.ts" in paths
    # README.md (no symbols, recognized text) is still analyzed, not dropped
    assert "README.md" in paths


def test_build_file_infos_inventories_binaries_for_completeness(tmp_path) -> None:
    # a fake repo with a code file, an unknown-extension text file, and a binary
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "notes.weirdext").write_text("just some text in an unknown extension\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x00\x00binary")
    infos, assets = build_file_infos(tmp_path, [])
    analyzed = {i.relative for i in infos}
    inventoried = {a.relative for a in assets}
    assert "a.py" in analyzed
    assert "notes.weirdext" in analyzed  # unknown extension, but text -> analyzed
    assert "logo.png" in inventoried  # binary -> inventoried, never dropped
    assert analyzed | inventoried == {"a.py", "notes.weirdext", "logo.png"}  # 100% accounted


def test_plan_from_static_produces_alpha_team_with_workers(tmp_path) -> None:
    _src, report = _make_report()
    output_root = tmp_path / "decode-out"
    static_path = tmp_path / "report.json"
    static_path.write_text(report.model_dump_json())

    plan = plan_from_static(
        report,
        slug="sample_repo",
        output_root=output_root,
        static_report_path=static_path,
    )

    assert plan.version == ORCHESTRATION_VERSION
    assert plan.slug == "sample_repo"
    assert plan.tier == report.tier.tier
    # Nano tier still produces at least one worker (single-agent fallback).
    assert plan.teams, "plan must include Team Alpha"
    alpha = plan.teams[0]
    assert alpha.id == "alpha"
    assert alpha.workers, "alpha must have at least one worker"
    worker = alpha.workers[0]
    assert "purpose" in worker.prompt and "risks" in worker.prompt  # decomposed slots
    assert "src/main.py" in worker.prompt or any("src/main.py" in f for f in worker.scope)


def test_cli_decode_writes_plan_and_index(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["decode", "--no-history", str(FIXTURE)]
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    out_dir = tmp_path / "docs" / "decode" / FIXTURE.name
    assert (out_dir / "static" / "report.json").exists()
    assert (out_dir / "plan.json").exists()
    assert (out_dir / "index.md").exists()
    assert (out_dir / "modules").is_dir()

    plan_data = json.loads((out_dir / "plan.json").read_text())
    assert plan_data["slug"] == FIXTURE.name
    assert plan_data["teams"][0]["id"] == "alpha"


def test_cli_decode_plan_only_emits_json_to_stdout(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["decode", "--no-history", "--plan-only", str(FIXTURE)]
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    assert payload["slug"] == FIXTURE.name
    assert payload["teams"][0]["id"] == "alpha"
