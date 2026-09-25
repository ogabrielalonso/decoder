from __future__ import annotations

from pathlib import Path

from decoder.execution import (
    ClaudeCliExecutor,
    CodexCliExecutor,
    MockExecutor,
    ModelTier,
    WorkerResult,
    WorkerSpec,
)
from decoder.execution.base import DENIED_TOOLS
from decoder.execution.claude_cli import _parse_claude_json


def _spec(**kw) -> WorkerSpec:
    base = dict(
        id="alpha-0",
        prompt="analyse this",
        tier=ModelTier.CHEAP,
        source_dir=Path("/src/repo"),
        output_file=Path("/out/modules/alpha-0.md"),
    )
    base.update(kw)
    return WorkerSpec(**base)


def test_worker_spec_dedupes_read_dirs() -> None:
    spec = _spec(extra_read_dirs=[Path("/src/repo"), Path("/out")])
    assert spec.all_read_dirs() == [Path("/src/repo"), Path("/out")]


def test_claude_command_blocks_write_and_sets_model() -> None:
    cmd = ClaudeCliExecutor().build_command(_spec(tier=ModelTier.CHEAP))
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--model" in cmd and "haiku" in cmd
    # never --bare (breaks subscription auth)
    assert "--bare" not in cmd
    # write tools explicitly denied (allowedTools alone does not block them)
    assert "--disallowedTools" in cmd
    for tool in DENIED_TOOLS:
        assert tool in cmd
    # json output for parsing cost/usage
    assert "json" in cmd


def test_claude_tier_maps_to_models() -> None:
    ex = ClaudeCliExecutor()
    assert ex.model_for(ModelTier.CHEAP) == "haiku"
    assert ex.model_for(ModelTier.BALANCED) == "sonnet"
    assert ex.model_for(ModelTier.STRONG) == "opus"


def test_model_override_wins_over_tier() -> None:
    # explicit model pins any model name (used by the eval harness)
    assert ClaudeCliExecutor(model="opus").model_for(ModelTier.CHEAP) == "opus"
    assert CodexCliExecutor(model="gpt-5.5").model_for(ModelTier.BALANCED) == "gpt-5.5"
    cmd = ClaudeCliExecutor(model="opus").build_command(_spec(tier=ModelTier.CHEAP))
    assert "opus" in cmd


def test_claude_adds_each_read_dir() -> None:
    spec = _spec(extra_read_dirs=[Path("/out")])
    cmd = ClaudeCliExecutor().build_command(spec)
    assert cmd.count("--add-dir") == 2
    assert "/src/repo" in cmd and "/out" in cmd


def test_codex_command_is_read_only_sandbox() -> None:
    spec = _spec(tier=ModelTier.BALANCED)
    msg = CodexCliExecutor._msg_file_for(spec)
    cmd = CodexCliExecutor().build_command(spec)
    assert cmd[:2] == ["codex", "exec"]
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--output-last-message" in cmd and str(msg) in cmd
    assert "--cd" in cmd and "/src/repo" in cmd


def test_parse_claude_json_extracts_text_and_cost() -> None:
    payload = '{"type":"result","subtype":"success","result":"# Arch","total_cost_usd":0.012}'
    text, cost, kind, _ = _parse_claude_json(payload)
    assert text == "# Arch"
    assert cost == 0.012
    assert kind == "ok"


def test_parse_claude_json_falls_back_to_raw_on_bad_json() -> None:
    text, cost, kind, _ = _parse_claude_json("not json at all")
    assert text == "not json at all"
    assert cost is None
    assert kind == "ok"  # unknown shape: trust the contract validator downstream


def test_parse_claude_json_empty_stdout_is_rate_limit() -> None:
    # Empty stdout with a zero exit is the classic silent rate cap.
    text, cost, kind, _ = _parse_claude_json("   ")
    assert (text, cost, kind) == ("", None, "rate_limit")


def test_parse_claude_json_success_envelope_empty_body_is_rate_limit() -> None:
    # The bug that silently dropped 13 workers: success envelope, empty result.
    payload = '{"type":"result","subtype":"success","result":"","total_cost_usd":0.0}'
    _, _, kind, _ = _parse_claude_json(payload)
    assert kind == "rate_limit"


def test_parse_claude_json_is_error_rate_limit() -> None:
    payload = '{"is_error":true,"subtype":"error","api_error_status":429,"result":"rate limit exceeded"}'
    _, _, kind, _ = _parse_claude_json(payload)
    assert kind == "rate_limit"


def test_parse_claude_json_is_error_hard() -> None:
    payload = '{"is_error":true,"subtype":"error_during_execution","result":"boom"}'
    _, _, kind, detail = _parse_claude_json(payload)
    assert kind == "error"
    assert detail == "boom"


def test_mock_executor_records_calls_and_returns_ok() -> None:
    ex = MockExecutor(responder=lambda s: f"done:{s.id}")
    result = ex.execute(_spec())
    assert isinstance(result, WorkerResult)
    assert result.ok
    assert result.output_text == "done:alpha-0"
    assert ex.calls and ex.calls[0].id == "alpha-0"


def test_worker_result_ok_property() -> None:
    assert WorkerResult(id="x", status="ok").ok
    assert not WorkerResult(id="x", status="error").ok
    assert not WorkerResult(id="x", status="timeout").ok
