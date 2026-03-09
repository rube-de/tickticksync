import click
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from click.testing import CliRunner
from tickticksync.cli import cli, _build_engine
from tickticksync.config import Config, MappingConfig, ProjectMapping, SyncConfig, TickTickConfig


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
# _run_oauth_flow helper
# ---------------------------------------------------------------------------

def test_run_oauth_flow_returns_token_path(tmp_path):
    """_run_oauth_flow runs the browser flow and returns the token path."""
    from tickticksync.cli import _run_oauth_flow
    from tickticksync.config import TickTickConfig
    from io import BytesIO

    token_path = tmp_path / "token.json"
    tt_cfg = TickTickConfig(client_id="cid", client_secret="csec")

    mock_handler = MagicMock()
    mock_handler.get_authorization_url.return_value = ("https://auth.url", "st8")
    mock_token = MagicMock()
    mock_token.access_token = "tok123"
    mock_handler.exchange_code = AsyncMock(return_value=mock_token)

    # Capture the handler class passed to HTTPServer so we can simulate
    # a callback request that populates the ``captured`` dict.
    handler_cls_holder = {}

    def _fake_http_server(addr, handler_cls):
        handler_cls_holder["cls"] = handler_cls
        return MagicMock()

    def _fake_wait(timeout=None):
        # Simulate the browser redirect hitting the callback server by
        # instantiating the handler class with a mock request.
        cls = handler_cls_holder["cls"]
        fake_request = MagicMock()
        fake_request.makefile.return_value = BytesIO(
            b"GET /callback?code=authcode123&state=st8 HTTP/1.1\r\n\r\n"
        )
        cls(fake_request, ("127.0.0.1", 9999), MagicMock())
        return True

    mock_event = MagicMock()
    mock_event.wait = _fake_wait

    with (
        patch("tickticksync.cli.webbrowser.open"),
        patch("tickticksync.cli.OAuth2Handler", return_value=mock_handler),
        patch("tickticksync.cli.http.server.HTTPServer", side_effect=_fake_http_server),
        patch("tickticksync.cli.threading.Thread"),
        patch("tickticksync.cli.threading.Event", return_value=mock_event),
        patch("tickticksync.cli.asyncio.run", return_value=mock_token),
    ):
        result_path = _run_oauth_flow(tt_cfg, token_path)

    assert result_path == token_path
    assert token_path.exists()
    import json
    assert json.loads(token_path.read_text())["access_token"] == "tok123"


# ---------------------------------------------------------------------------
# _fetch_ticktick_projects helper
# ---------------------------------------------------------------------------

def test_fetch_ticktick_projects_returns_list():
    """_fetch_ticktick_projects returns the project list from the API."""
    from tickticksync.cli import _fetch_ticktick_projects

    mock_api = MagicMock()
    mock_api.connect = AsyncMock()
    mock_api.disconnect = AsyncMock()
    mock_api.get_projects = AsyncMock(return_value=[
        {"id": "1", "name": "Inbox"},
        {"id": "2", "name": "Work"},
    ])

    projects = _fetch_ticktick_projects(mock_api)
    assert len(projects) == 2
    assert projects[0]["name"] == "Inbox"
    mock_api.connect.assert_awaited_once()
    mock_api.disconnect.assert_awaited_once()


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


# ---------------------------------------------------------------------------
# init wizard
# ---------------------------------------------------------------------------

def test_init_fresh_prompts_credentials(runner, tmp_path):
    """Fresh init prompts for client_id and client_secret."""
    config_path = tmp_path / "config.toml"

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("OAuth skipped")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="my_client_id\nmy_secret\ny\n\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "Step 1/4" in result.output
    save_kwargs = mock_save.call_args[1]
    assert save_kwargs["client_id"] == "my_client_id"
    assert save_kwargs["client_secret"] == "my_secret"


def test_init_fresh_sync_settings_defaults(runner, tmp_path):
    """Init step 3 accepts default sync settings when user presses Enter."""
    config_path = tmp_path / "config.toml"

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("OAuth skipped")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="cid\ncsec\ny\n\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "Step 3/4" in result.output
    save_kwargs = mock_save.call_args[1]
    assert save_kwargs["poll_interval"] == SyncConfig.poll_interval
    assert save_kwargs["socket_path"] == SyncConfig.socket_path


def test_init_fresh_sync_settings_custom(runner, tmp_path):
    """Init step 3 accepts custom sync settings."""
    config_path = tmp_path / "config.toml"

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("OAuth skipped")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="cid\ncsec\ny\n120\n/tmp/custom.sock\ny\n")

    assert result.exit_code == 0, result.output
    save_kwargs = mock_save.call_args[1]
    assert save_kwargs["poll_interval"] == 120
    assert save_kwargs["socket_path"] == "/tmp/custom.sock"


def test_init_fresh_oauth_success(runner, tmp_path):
    """Init step 2 runs OAuth flow and reports success."""
    config_path = tmp_path / "config.toml"
    token_path = tmp_path / "token.json"

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", return_value=token_path) as mock_oauth,
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="cid\ncsec\n\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "Step 2/4" in result.output
    mock_oauth.assert_called_once()
    save_kwargs = mock_save.call_args[1]
    assert save_kwargs["auth_method"] == "oauth"


def test_init_oauth_failure_continues(runner, tmp_path):
    """If OAuth fails, init continues and tells user to run auth later."""
    config_path = tmp_path / "config.toml"

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full"),
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("No OAuth code received")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="cid\ncsec\ny\n\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "tickticksync auth oauth" in result.output


def test_init_fresh_mapping_wizard(runner, tmp_path):
    """Init step 4 fetches projects and allows mapping selection."""
    config_path = tmp_path / "config.toml"
    token_path = tmp_path / "token.json"

    mock_api = MagicMock()

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", return_value=token_path),
        patch("tickticksync.cli._build_api", return_value=mock_api),
        patch("tickticksync.cli._fetch_ticktick_projects", return_value=[
            {"id": "1", "name": "Inbox"},
            {"id": "2", "name": "Work"},
            {"id": "3", "name": "Personal"},
        ]),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        # Input: cid, csec, poll(default), socket(default),
        # mapping: add? y, select 1 (Inbox), accept default, add more? y,
        # select 1 (Work), accept default, add more? n
        result = runner.invoke(
            cli, ["init"],
            input="cid\ncsec\n\n\ny\n1\n\ny\n1\n\nn\n",
        )

    assert result.exit_code == 0, result.output
    assert "Step 4/4" in result.output
    save_kwargs = mock_save.call_args[1]
    projects = save_kwargs["projects"]
    assert len(projects) == 2
    assert projects[0].ticktick == "Inbox"
    assert projects[0].taskwarrior == "inbox"
    assert projects[1].ticktick == "Work"
    assert projects[1].taskwarrior == "work"


def test_init_skip_mapping(runner, tmp_path):
    """Init step 4 can be skipped entirely."""
    config_path = tmp_path / "config.toml"

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("OAuth skipped")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="cid\ncsec\ny\n\n\ny\n")

    assert result.exit_code == 0, result.output
    save_kwargs = mock_save.call_args[1]
    assert save_kwargs["projects"] == []


def test_init_existing_config_asks_reconfigure(runner, tmp_path):
    """Re-running init on existing config asks before overwriting."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[ticktick]\nclient_id = "old_cid"\nclient_secret = "old_csec"\n')

    existing_cfg = Config(
        ticktick=TickTickConfig(client_id="old_cid", client_secret="old_csec"),
    )

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.load_config", return_value=existing_cfg),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("skip")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="y\nnew_cid\nnew_csec\ny\n\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "already exists" in result.output.lower()
    save_kwargs = mock_save.call_args[1]
    assert save_kwargs["client_id"] == "new_cid"


def test_init_reconfigure_seeds_from_existing(runner, tmp_path):
    """Re-running init with reconfigure pre-populates prompts from existing config."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[ticktick]\nclient_id = "old_cid"\nclient_secret = "old_csec"\n'
        '[sync]\npoll_interval = 90\nsocket_path = "/tmp/old.sock"\n'
        '[mapping]\ndefault_project = "work"\n'
        '[[mapping.projects]]\nticktick = "Inbox"\ntaskwarrior = "inbox"\n'
    )

    existing_cfg = Config(
        ticktick=TickTickConfig(client_id="old_cid", client_secret="old_csec"),
        sync=SyncConfig(poll_interval=90, socket_path="/tmp/old.sock"),
        mapping=MappingConfig(
            default_project="work",
            projects=[ProjectMapping(ticktick="Inbox", taskwarrior="inbox")],
        ),
    )

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.load_config", return_value=existing_cfg),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("skip")),
        patch("tickticksync.cli._build_api", return_value=MagicMock()),
        patch("tickticksync.cli._fetch_ticktick_projects", side_effect=Exception("no auth")),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        # User says yes to reconfigure, then accepts all defaults (presses Enter for each prompt)
        result = runner.invoke(cli, ["init"], input="y\n\n\ny\n\n\ny\n")

    assert result.exit_code == 0, result.output
    save_kwargs = mock_save.call_args[1]
    # Existing values preserved when user accepts defaults
    assert save_kwargs["client_id"] == "old_cid"
    assert save_kwargs["client_secret"] == "old_csec"
    assert save_kwargs["poll_interval"] == 90
    assert save_kwargs["socket_path"] == "/tmp/old.sock"
    assert save_kwargs["default_project"] == "work"


def test_init_reconfigure_preserves_existing_mappings(runner, tmp_path):
    """Re-running init pre-populates existing mappings."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[ticktick]\nclient_id = "cid"\nclient_secret = "csec"\n'
        '[[mapping.projects]]\nticktick = "Inbox"\ntaskwarrior = "inbox"\n'
    )

    existing_cfg = Config(
        ticktick=TickTickConfig(client_id="cid", client_secret="csec"),
        mapping=MappingConfig(
            projects=[ProjectMapping(ticktick="Inbox", taskwarrior="inbox")],
        ),
    )

    mock_api = MagicMock()

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.load_config", return_value=existing_cfg),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli._run_oauth_flow", side_effect=click.ClickException("skip")),
        patch("tickticksync.cli._build_api", return_value=mock_api),
        patch("tickticksync.cli._fetch_ticktick_projects", return_value=[
            {"id": "1", "name": "Inbox"},
            {"id": "2", "name": "Work"},
        ]),
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        # Reconfigure: y, creds: Enter+Enter (accept defaults), oauth skip: y,
        # sync: Enter+Enter (accept defaults), mapping: don't add more: n
        result = runner.invoke(cli, ["init"], input="y\n\n\ny\n\n\nn\n")

    assert result.exit_code == 0, result.output
    save_kwargs = mock_save.call_args[1]
    projects = save_kwargs["projects"]
    # Existing mapping preserved even though user didn't re-add it
    assert any(p.ticktick == "Inbox" and p.taskwarrior == "inbox" for p in projects)


def test_init_existing_config_skip_reconfigure(runner, tmp_path):
    """Re-running init with 'n' skips to UDA/hooks (idempotent steps)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[ticktick]\nclient_id = "cid"\nclient_secret = "csec"\n')

    with (
        patch("tickticksync.cli.DEFAULT_CONFIG_PATH", config_path),
        patch("tickticksync.cli.save_config_full") as mock_save,
        patch("tickticksync.cli.TaskWarriorClient") as mock_tw_cls,
        patch("tickticksync.cli.HOOKS_DIR", tmp_path / "hooks"),
    ):
        mock_tw_cls.return_value = MagicMock()
        result = runner.invoke(cli, ["init"], input="n\n")

    assert result.exit_code == 0, result.output
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# _build_engine
# ---------------------------------------------------------------------------

def test_build_engine_passes_project_mappings():
    """_build_engine passes project mappings from config to SyncEngine."""
    mappings = [ProjectMapping(ticktick="Inbox", taskwarrior="inbox")]
    cfg = Config(
        ticktick=TickTickConfig(client_id="id", client_secret="secret"),
        mapping=MappingConfig(default_project="inbox", projects=mappings),
    )
    with patch("tickticksync.cli.StateStore"), \
         patch("tickticksync.cli.TaskWarriorClient"), \
         patch("tickticksync.cli._build_api"):
        engine = _build_engine(cfg)
    assert engine._tt_to_tw == {"Inbox": "inbox"}
    assert engine._tw_to_tt == {"inbox": "Inbox"}
