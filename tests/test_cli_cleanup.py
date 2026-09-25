from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from decoder.cli import app
from decoder.config import Tier
from decoder.schemas import RepoMetrics, StaticAnalysisReport, TierDecision

runner = CliRunner()


def _write_report(slug_dir: Path, root: Path) -> None:
    static = slug_dir / "static"
    static.mkdir(parents=True)
    rep = StaticAnalysisReport(
        metrics=RepoMetrics(root=root.resolve()),
        tier=TierDecision(tier=Tier.NANO, reasoning="t", teams=1, workers_per_team=1),
    )
    (static / "report.json").write_text(rep.model_dump_json())


def test_cleanup_removes_managed_clone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    clone = tmp_path / "workspace" / "myrepo"
    clone.mkdir(parents=True)
    (clone / "f.py").write_text("x = 1\n")
    _write_report(tmp_path / "docs" / "decode" / "myrepo", clone)  # source == clone
    r = runner.invoke(app, ["cleanup", "myrepo"])
    assert r.exit_code == 0, r.stdout
    assert not clone.exists()  # the clone was removed


def test_cleanup_never_touches_local_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "my_local_project"  # NOT under workspace/
    local.mkdir()
    (local / "f.py").write_text("x = 1\n")
    _write_report(tmp_path / "docs" / "decode" / "my_local_project", local)
    r = runner.invoke(app, ["cleanup", "my_local_project"])
    assert r.exit_code == 0, r.stdout
    assert local.exists()  # a local source is never deleted
