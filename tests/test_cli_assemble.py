from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from decoder.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
runner = CliRunner()


def _plan_alpha_worker(tmp_path: Path) -> dict:
    r = runner.invoke(app, ["decode", "--plan-only", "--no-history", str(FIXTURE)])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")
    plan = json.loads((tmp_path / "docs" / "decode" / "sample_repo" / "plan.json").read_text())
    return next(t for t in plan["teams"] if t["id"] == "alpha")["workers"][0]


def _raw_json(scope: list[str]) -> str:
    return json.dumps({p: {"purpose": f"purpose of {p}", "symbols": {}, "risks": []} for p in scope})


def test_assemble_alpha_tolerates_append_convention(tmp_path: Path, monkeypatch) -> None:
    # The skill notation could yield 'alpha-worker-00.md.raw.json' (append). The
    # durable tool fix must still find it instead of silently emitting a skeleton.
    monkeypatch.chdir(tmp_path)
    w = _plan_alpha_worker(tmp_path)
    out = Path(w["output_file"])
    Path(f"{out}.raw.json").write_text(_raw_json(w["scope"]))  # append convention
    r = runner.invoke(app, ["assemble-alpha", "sample_repo"])
    assert r.exit_code == 0, r.stdout
    assert "assembled 1" in r.stdout
    assert "purpose of" in out.read_text()


def test_assemble_alpha_tolerates_with_suffix_convention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    w = _plan_alpha_worker(tmp_path)
    out = Path(w["output_file"])
    out.with_suffix(".raw.json").write_text(_raw_json(w["scope"]))  # with_suffix convention
    r = runner.invoke(app, ["assemble-alpha", "sample_repo"])
    assert r.exit_code == 0 and "assembled 1" in r.stdout


def test_assemble_alpha_missing_json_degrades_to_skeleton(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    w = _plan_alpha_worker(tmp_path)
    out = Path(w["output_file"])
    r = runner.invoke(app, ["assemble-alpha", "sample_repo"])  # no raw json saved at all
    assert r.exit_code == 0
    assert "skeleton-only" in r.stdout
    assert out.exists()  # skeleton written deterministically, never a crash
    assert "### Key symbols" in out.read_text()


def test_dump_prompts_writes_prompts_and_manifest(tmp_path: Path, monkeypatch) -> None:
    # dump-prompts must persist every worker's injected prompt to disk and a
    # manifest the skill/Workflow consumes: alpha -> write .raw.json, synth -> .md.
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["decode", "--plan-only", "--no-history", str(FIXTURE)]).exit_code == 0
    r = runner.invoke(app, ["dump-prompts", "sample_repo"])
    assert r.exit_code == 0, r.stdout
    base = tmp_path / "docs" / "decode" / "sample_repo" / "prompts"
    manifest = json.loads((base / "manifest.json").read_text())
    assert manifest
    assert any(m["team"] == "alpha" for m in manifest)
    assert any(m["kind"] == "synth" for m in manifest)
    for m in manifest:
        assert Path(m["prompt"]).exists()
        assert m["output"].endswith(".raw.json" if m["kind"] == "alpha" else ".md")


def test_dump_prompts_team_filter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["decode", "--plan-only", "--no-history", str(FIXTURE)]).exit_code == 0
    r = runner.invoke(app, ["dump-prompts", "sample_repo", "--teams", "alpha"])
    assert r.exit_code == 0, r.stdout
    base = tmp_path / "docs" / "decode" / "sample_repo" / "prompts"
    manifest = json.loads((base / "manifest.json").read_text())
    assert manifest and all(m["team"] == "alpha" for m in manifest)


def test_build_context_pack_condenses_modules(tmp_path: Path, monkeypatch) -> None:
    # After assemble-alpha, build-context-pack must condense modules/ into a
    # single context_pack.md the synthesis teams read instead of every module.
    monkeypatch.chdir(tmp_path)
    w = _plan_alpha_worker(tmp_path)
    out = Path(w["output_file"])
    Path(f"{out}.raw.json").write_text(_raw_json(w["scope"]))
    assert runner.invoke(app, ["assemble-alpha", "sample_repo"]).exit_code == 0
    r = runner.invoke(app, ["build-context-pack", "sample_repo"])
    assert r.exit_code == 0, r.stdout
    pack = tmp_path / "docs" / "decode" / "sample_repo" / "context_pack.md"
    assert pack.exists() and pack.read_text().strip()
