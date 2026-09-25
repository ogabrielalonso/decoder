import json
from pathlib import Path

from typer.testing import CliRunner

from decoder.cli import app
from decoder.ingestion.cloner import resolve_source
from decoder.ingestion.metrics import compute_metrics
from decoder.ingestion.tier import decide_tier
from decoder.orchestration.event_bus import Event, EventBus
from decoder.orchestration.master import plan_from_static
from decoder.static_analysis.pipeline import run_static_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _make_report():
    src = resolve_source(str(FIXTURE))
    metrics = compute_metrics(src)
    tier = decide_tier(metrics)
    return run_static_analysis(src.path, metrics, tier, include_history=False)


def test_event_bus_appends_and_reads(tmp_path: Path) -> None:
    bus = EventBus(tmp_path / "events.jsonl")
    bus.publish(Event(team="alpha", worker_id="alpha-0", type="scope.started", payload={"n": 3}))
    bus.publish(Event(team="bravo", worker_id="bravo-0", type="pattern.detected", payload={"p": "factory"}))

    all_events = bus.events()
    assert len(all_events) == 2
    assert bus.by_team("alpha")[0]["worker_id"] == "alpha-0"
    assert bus.by_team("bravo")[0]["type"] == "pattern.detected"


def test_event_bus_tolerates_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"team": "a", "worker_id": "w", "type": "t", "payload": {}}\nnot-json\n')
    bus = EventBus(path)
    assert len(bus.events()) == 1


def test_plan_includes_all_four_teams(tmp_path: Path) -> None:
    report = _make_report()
    plan = plan_from_static(
        report,
        slug="sample",
        output_root=tmp_path / "out",
        static_report_path=tmp_path / "out" / "static" / "report.json",
    )
    team_ids = [t.id for t in plan.teams]
    assert team_ids == ["alpha", "bravo", "charlie", "delta"]
    # Bravo/Charlie/Delta each have exactly one synthesis worker.
    for tid in ("bravo", "charlie", "delta"):
        team = next(t for t in plan.teams if t.id == tid)
        assert len(team.workers) == 1
        assert team.workers[0].id.endswith("-0")
        assert Path(team.workers[0].output_file).name in {
            "architecture.md",
            "domain.md",
            "audit.md",
        }


def test_plan_bravo_prompt_references_alpha_outputs(tmp_path: Path) -> None:
    report = _make_report()
    plan = plan_from_static(
        report,
        slug="sample",
        output_root=tmp_path / "out",
        static_report_path=tmp_path / "out" / "static" / "report.json",
    )
    bravo = next(t for t in plan.teams if t.id == "bravo")
    assert "modules/" in bravo.workers[0].prompt
    assert "index.md" in bravo.workers[0].prompt


def test_cli_decode_creates_events_jsonl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["decode", "--no-history", str(FIXTURE)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    events_path = tmp_path / "docs" / "decode" / FIXTURE.name / "events.jsonl"
    assert events_path.exists()


def test_cli_decode_plan_json_has_four_teams(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["decode", "--no-history", "--plan-only", str(FIXTURE)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    plan = json.loads(result.stdout)
    assert [t["id"] for t in plan["teams"]] == ["alpha", "bravo", "charlie", "delta"]
