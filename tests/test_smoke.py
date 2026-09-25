from typer.testing import CliRunner

from decoder import __version__
from decoder.cli import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
    assert __version__ == "1.1.0"


def test_info_command_lists_tiers() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "nano" in result.stdout.lower()
    assert "huge" in result.stdout.lower()


def test_decode_help_is_registered() -> None:
    result = runner.invoke(app, ["decode", "--help"])
    assert result.exit_code == 0
    assert "static-only" in result.stdout


def test_decode_compare_help_is_registered() -> None:
    result = runner.invoke(app, ["decode-compare", "--help"])
    assert result.exit_code == 0
    assert "insights-prompt" in result.stdout
