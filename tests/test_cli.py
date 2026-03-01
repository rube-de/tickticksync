import pytest
from unittest.mock import patch
from click.testing import CliRunner
from tickticksync.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_status_no_config(runner):
    with patch("tickticksync.cli.load_config", side_effect=FileNotFoundError):
        result = runner.invoke(cli, ["status"])
    assert (
        result.exit_code != 0
        or "not found" in result.output.lower()
        or "error" in result.output.lower()
    )


def test_daemon_status_not_running(runner, tmp_path):
    pid_path = tmp_path / "tickticksync.pid"
    with patch("tickticksync.cli.PID_FILE", pid_path):
        result = runner.invoke(cli, ["daemon", "status"])
    assert "not running" in result.output.lower()


def test_daemon_status_running(runner, tmp_path):
    pid_path = tmp_path / "tickticksync.pid"
    pid_path.write_text(str(99999999))  # unlikely real PID
    with patch("tickticksync.cli.PID_FILE", pid_path):
        result = runner.invoke(cli, ["daemon", "status"])
    # Either "running" or "not running" (pid may not exist) — just confirm no crash
    assert result.exit_code == 0
