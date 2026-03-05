import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from tickticksync.cli import cli
from tickticksync.config import Config, ProjectMapping, TickTickConfig


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


# ---------------------------------------------------------------------------
# auth password
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> tuple[Path, Config]:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[ticktick]\nclient_id = "cid"\nclient_secret = "csec"\n')
    cfg = Config(ticktick=TickTickConfig(client_id="cid", client_secret="csec"))
    return config_path, cfg


def test_auth_password_stores_keyring(runner, tmp_path):
    config_path, cfg = _make_cfg(tmp_path)

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.save_config_auth") as mock_save,
        patch("tickticksync.cli.keyring.set_password") as mock_set,
        patch("tickticksync.cli.keyring.errors.NoKeyringError", Exception),
    ):
        result = runner.invoke(
            cli,
            ["auth", "password"],
            input="user@example.com\nsecretpw\n",
        )

    assert result.exit_code == 0, result.output
    mock_set.assert_called_once_with("tickticksync", "user@example.com", "secretpw")
    # config_path inside the command is hardcoded to ~/.config/tickticksync/config.toml;
    # we verify method and username without asserting the exact path.
    args = mock_save.call_args[0]
    assert args[1] == "password"
    assert args[2] == "user@example.com"


def test_auth_password_no_keyring(runner, tmp_path):
    _, cfg = _make_cfg(tmp_path)

    import keyring.errors as kr_errors

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch(
            "tickticksync.cli.keyring.set_password",
            side_effect=kr_errors.NoKeyringError,
        ),
    ):
        result = runner.invoke(
            cli,
            ["auth", "password"],
            input="user@example.com\nsecretpw\n",
        )

    assert result.exit_code != 0
    assert "keyring" in result.output.lower()


# ---------------------------------------------------------------------------
# auth oauth
# ---------------------------------------------------------------------------

def test_auth_oauth_prints_url_and_waits(runner, tmp_path):
    """Verify the auth URL is printed and webbrowser.open is called."""
    _, cfg = _make_cfg(tmp_path)

    mock_handler = MagicMock()
    mock_handler.get_authorization_url.return_value = (
        "https://ticktick.com/oauth/authorize?foo=bar", "st8"
    )

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.webbrowser.open") as mock_open,
        patch("tickticksync.cli.OAuth2Handler", return_value=mock_handler),
        patch("tickticksync.cli.http.server.HTTPServer", return_value=MagicMock()),
        patch("tickticksync.cli.threading.Thread"),
        patch(
            "tickticksync.cli.threading.Event",
            return_value=MagicMock(wait=MagicMock(return_value=True)),
        ),
    ):
        result = runner.invoke(cli, ["auth", "oauth"])

    mock_open.assert_called_once_with("https://ticktick.com/oauth/authorize?foo=bar")
    assert "ticktick.com" in result.output


def test_auth_oauth_timeout_exits_with_error(runner, tmp_path):
    """When no callback arrives (done.wait returns False), report a clear error."""
    _, cfg = _make_cfg(tmp_path)

    mock_handler = MagicMock()
    mock_handler.get_authorization_url.return_value = ("https://auth.example.com/oauth", "st8")

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.webbrowser.open"),
        patch("tickticksync.cli.OAuth2Handler", return_value=mock_handler),
        patch("tickticksync.cli.http.server.HTTPServer", return_value=MagicMock()),
        patch("tickticksync.cli.threading.Thread"),
        patch(
            "tickticksync.cli.threading.Event",
            return_value=MagicMock(wait=MagicMock(return_value=False)),
        ),
    ):
        result = runner.invoke(cli, ["auth", "oauth"])

    assert result.exit_code == 1
    assert "No OAuth code" in result.output


# ---------------------------------------------------------------------------
# mapping list
# ---------------------------------------------------------------------------

def test_mapping_list_shows_table(runner, tmp_path):
    """mapping list with configured projects shows a formatted table."""
    config_path, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [
        ProjectMapping(ticktick="Inbox", taskwarrior="inbox"),
        ProjectMapping(ticktick="Work", taskwarrior="work"),
    ]
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(cli, ["mapping", "list"])
    assert result.exit_code == 0
    assert "Inbox" in result.output
    assert "inbox" in result.output
    assert "Work" in result.output
    assert "2 mappings" in result.output


def test_mapping_list_empty(runner, tmp_path):
    """mapping list with no mappings shows a helpful message."""
    _, cfg = _make_cfg(tmp_path)
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(cli, ["mapping", "list"])
    assert result.exit_code == 0
    assert "No project mappings configured" in result.output


# ---------------------------------------------------------------------------
# mapping remove
# ---------------------------------------------------------------------------

def test_mapping_remove_existing(runner, tmp_path):
    """mapping remove deletes a mapping and persists the change."""
    config_path, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [
        ProjectMapping(ticktick="Inbox", taskwarrior="inbox"),
        ProjectMapping(ticktick="Work", taskwarrior="work"),
    ]
    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_mapping") as mock_save,
    ):
        result = runner.invoke(cli, ["mapping", "remove", "Work"])
    assert result.exit_code == 0
    assert "Removed mapping" in result.output
    assert "Work" in result.output
    saved_projects = mock_save.call_args[0][1]
    assert len(saved_projects) == 1
    assert saved_projects[0].ticktick == "Inbox"


def test_mapping_remove_nonexistent(runner, tmp_path):
    """mapping remove for a non-existent mapping shows an error."""
    _, cfg = _make_cfg(tmp_path)
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(cli, ["mapping", "remove", "Nonexistent"])
    assert result.exit_code != 0
    assert "No mapping found" in result.output


# ---------------------------------------------------------------------------
# mapping add
# ---------------------------------------------------------------------------

def test_mapping_add_non_interactive(runner, tmp_path):
    """mapping add --ticktick X --taskwarrior y adds the mapping."""
    config_path, cfg = _make_cfg(tmp_path)
    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_mapping") as mock_save,
    ):
        result = runner.invoke(
            cli, ["mapping", "add", "--ticktick", "Work", "--taskwarrior", "work"]
        )
    assert result.exit_code == 0
    assert "Work" in result.output
    assert "work" in result.output
    saved = mock_save.call_args[0][1]
    assert any(p.ticktick == "Work" and p.taskwarrior == "work" for p in saved)


def test_mapping_add_non_interactive_duplicate(runner, tmp_path):
    """mapping add for an already-mapped TickTick project shows an error."""
    _, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(
            cli, ["mapping", "add", "--ticktick", "Work", "--taskwarrior", "other"]
        )
    assert result.exit_code != 0
    assert "already mapped" in result.output.lower()
