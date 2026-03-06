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
    _, cfg = _make_cfg(tmp_path)
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


def test_mapping_list_singular(runner, tmp_path):
    """mapping list with exactly one mapping shows '1 mapping' (singular)."""
    _, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [ProjectMapping(ticktick="Inbox", taskwarrior="inbox")]
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(cli, ["mapping", "list"])
    assert result.exit_code == 0
    assert "1 mapping" in result.output
    assert "1 mappings" not in result.output


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


def test_mapping_add_interactive(runner, tmp_path):
    """Interactive mapping add fetches projects, shows unmapped, prompts user."""
    from unittest.mock import AsyncMock

    config_path, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [ProjectMapping(ticktick="Inbox", taskwarrior="inbox")]

    mock_api = MagicMock()
    mock_api.connect = AsyncMock()
    mock_api.disconnect = AsyncMock()
    mock_api.get_projects = AsyncMock(return_value=[
        {"id": "1", "name": "Inbox"},
        {"id": "2", "name": "Personal"},
        {"id": "3", "name": "Shopping"},
    ])

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_mapping") as mock_save,
        patch("tickticksync.cli._build_api", return_value=mock_api),
    ):
        # Select project 1 (Personal), accept default TW name
        result = runner.invoke(cli, ["mapping", "add"], input="1\n\n")

    assert result.exit_code == 0, result.output
    assert "Personal" in result.output
    saved = mock_save.call_args[0][1]
    new_mappings = [p for p in saved if p.ticktick == "Personal"]
    assert len(new_mappings) == 1
    assert new_mappings[0].taskwarrior == "personal"


def test_mapping_add_interactive_no_unmapped(runner, tmp_path):
    """When all projects are already mapped, show a message."""
    from unittest.mock import AsyncMock

    _, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [
        ProjectMapping(ticktick="Inbox", taskwarrior="inbox"),
        ProjectMapping(ticktick="Work", taskwarrior="work"),
    ]

    mock_api = MagicMock()
    mock_api.connect = AsyncMock()
    mock_api.disconnect = AsyncMock()
    mock_api.get_projects = AsyncMock(return_value=[
        {"id": "1", "name": "Inbox"},
        {"id": "2", "name": "Work"},
    ])

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli._build_api", return_value=mock_api),
    ):
        result = runner.invoke(cli, ["mapping", "add"])

    assert result.exit_code == 0
    assert "all projects are already mapped" in result.output.lower()


def test_mapping_add_non_interactive_duplicate_taskwarrior(runner, tmp_path):
    """mapping add rejects a TaskWarrior project name already used by another mapping."""
    _, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(
            cli, ["mapping", "add", "--ticktick", "Personal", "--taskwarrior", "work"]
        )
    assert result.exit_code != 0
    assert "already used" in result.output.lower()


def test_mapping_add_partial_flags_rejected(runner, tmp_path):
    """mapping add with only one of --ticktick/--taskwarrior raises an error."""
    _, cfg = _make_cfg(tmp_path)
    with patch("tickticksync.cli.load_config", return_value=cfg):
        result = runner.invoke(
            cli, ["mapping", "add", "--ticktick", "Work"]
        )
    assert result.exit_code != 0
    assert "both" in result.output.lower()


def test_mapping_add_interactive_duplicate_taskwarrior(runner, tmp_path):
    """Interactive mode rejects a TaskWarrior name that's already in use."""
    from unittest.mock import AsyncMock

    config_path, cfg = _make_cfg(tmp_path)
    cfg.mapping.projects = [ProjectMapping(ticktick="Inbox", taskwarrior="inbox")]

    mock_api = MagicMock()
    mock_api.connect = AsyncMock()
    mock_api.disconnect = AsyncMock()
    mock_api.get_projects = AsyncMock(return_value=[
        {"id": "1", "name": "Inbox"},
        {"id": "2", "name": "Personal"},
    ])

    with (
        patch("tickticksync.cli.load_config", return_value=cfg),
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli._build_api", return_value=mock_api),
    ):
        # Select project 1 (Personal), enter "inbox" as TW name (already used)
        result = runner.invoke(cli, ["mapping", "add"], input="1\ninbox\n")

    assert result.exit_code != 0
    assert "already used" in result.output.lower()
