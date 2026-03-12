import asyncio
import dataclasses
import http.server
import json
import os
import signal
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
import keyring
import keyring.errors
from ticktick_sdk.auth_cli import OAuth2Handler

from .config import (
    DEFAULT_CONFIG_PATH,
    AuthConfig,
    Config,
    MappingConfig,
    ProjectMapping,
    SyncConfig,
    TickTickConfig,
    load_config,
    save_config_auth,
    save_config_full,
    save_config_mapping,
    update_config_value,
)
from .state import StateStore
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI
from .sync import SyncEngine
from .daemon import Daemon

PID_FILE = Path("~/.local/share/tickticksync/tickticksync.pid").expanduser()
HOOKS_DIR = Path("~/.local/share/task/hooks").expanduser()


_KEYRING_SERVICE = "tickticksync"
_OAUTH_REDIRECT_URI = "http://localhost:8080/callback"


def _build_engine(cfg: Config) -> SyncEngine:
    state = StateStore(cfg.db_path)
    tw = TaskWarriorClient()
    tt = _build_api(cfg)
    return SyncEngine(
        store=state,
        tw=tw,
        tt=tt,
        project_mappings=cfg.mapping.projects,
    )


def _build_api(cfg: Config) -> TickTickAPI:
    """Construct a TickTickAPI instance from config (no SyncEngine needed)."""
    api_args = (cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path))

    if cfg.auth.method == "password":
        username = cfg.auth.username
        if not username:
            raise click.ClickException(
                "No username configured. Run `tickticksync auth password`."
            )
        try:
            password = keyring.get_password(_KEYRING_SERVICE, username)
        except keyring.errors.NoKeyringError as err:
            raise click.ClickException(
                "No system keyring available. Run `tickticksync auth password` on a"
                " machine with a keyring (macOS Keychain or Linux Secret Service)."
            ) from err
        if not password:
            raise click.ClickException(
                f"No stored password for {username!r}. Run `tickticksync auth password`."
            )
        return TickTickAPI(*api_args, username=username, password=password, use_v2_tasks=True)
    return TickTickAPI(*api_args)


def _fetch_ticktick_projects(api: TickTickAPI) -> list[dict]:
    """Connect to TickTick, fetch all projects, then disconnect."""
    async def _fetch() -> list[dict]:
        await api.connect()
        try:
            return await api.get_projects()
        finally:
            await api.disconnect()

    return asyncio.run(_fetch())


def _read_pid() -> int | None:
    """Read PID from file and verify process exists. Cleans up stale files."""
    try:
        pid = int(PID_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        return None


@click.group()
def cli() -> None:
    """TickTick <-> TaskWarrior bidirectional sync."""


# ---------------------------------------------------------------------------
# auth group
# ---------------------------------------------------------------------------

@cli.group()
def auth() -> None:
    """Authenticate with TickTick (OAuth or password)."""


def _run_oauth_flow(tt_cfg: TickTickConfig, token_path: Path) -> Path:
    """Run the browser OAuth2 flow and save the access token to *token_path*.

    This is the reusable core shared by ``auth oauth`` and ``init``.
    It does **not** update the config auth method — callers handle that.
    """
    parsed_redirect = urlparse(_OAUTH_REDIRECT_URI)
    handler = OAuth2Handler(tt_cfg.client_id, tt_cfg.client_secret, _OAUTH_REDIRECT_URI)
    auth_url, state = handler.get_authorization_url()

    click.echo(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Capture the callback on localhost:8080
    captured: dict[str, str] = {}
    done = threading.Event()

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not parsed.path.startswith(parsed_redirect.path):
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            captured["code"] = params.get("code", [""])[0]
            captured["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Auth complete. You can close this tab.")
            done.set()

        def log_message(self, *_: object) -> None:  # suppress access logs
            pass

    server = http.server.HTTPServer((parsed_redirect.hostname, parsed_redirect.port), _CallbackHandler)
    click.echo(f"Waiting for OAuth callback on {_OAUTH_REDIRECT_URI} …")
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    received = done.wait(timeout=300)
    server.shutdown()

    if not received or not captured.get("code"):
        raise click.ClickException("No OAuth code received within 5 minutes.")

    if captured.get("state") != state:
        raise click.ClickException("Invalid OAuth state received; please retry authentication.")

    token = asyncio.run(handler.exchange_code(captured["code"], captured["state"]))

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({"access_token": token.access_token}))
    token_path.chmod(0o600)
    click.echo(f"Token saved to {token_path}")

    return token_path


@auth.command("oauth")
def auth_oauth() -> None:
    """Run the browser OAuth2 flow and save the access token."""
    config_path = DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)

    _run_oauth_flow(cfg.ticktick, cfg.token_path)

    save_config_auth(config_path, "oauth")
    click.echo("Auth method set to 'oauth' in config.")


@auth.command("password")
def auth_password() -> None:
    """Store TickTick username/password credentials in the system keyring."""
    config_path = DEFAULT_CONFIG_PATH
    load_config(config_path)  # validate config exists and is parseable

    username = click.prompt("TickTick username (email)")
    password = click.prompt("TickTick password", hide_input=True)

    try:
        keyring.set_password(_KEYRING_SERVICE, username, password)
    except keyring.errors.NoKeyringError:
        raise click.ClickException(
            "No system keyring available. Install a keyring backend"
            " (e.g. `uv add keyrings.alt`) or run on macOS/Linux with a"
            " Secret Service daemon."
        )

    save_config_auth(config_path, "password", username)
    click.echo(f"Password stored in keyring for {username!r}.")
    click.echo("Auth method set to 'password' in config.")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def _register_uda_and_hooks() -> None:
    """Register the TW UDA and install hook scripts (idempotent)."""
    tw = TaskWarriorClient()
    tw.register_uda("ticktickid", "string", "TickTick ID")
    click.echo("✓ Registered TW UDA: ticktickid")

    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for hook_name, entry in [
        ("on-add.tickticksync", "tickticksync.hooks:on_add_hook"),
        ("on-modify.tickticksync", "tickticksync.hooks:on_modify_hook"),
    ]:
        hook_path = HOOKS_DIR / hook_name
        module, func = entry.split(":")
        hook_path.write_text(
            f"#!/usr/bin/env python3\nfrom {module} import {func}\n{func}()\n"
        )
        hook_path.chmod(0o755)
    click.echo(f"✓ Hook scripts installed in {HOOKS_DIR}")


@cli.command()
def init() -> None:
    """Interactive setup wizard for TickTickSync."""
    config_path = DEFAULT_CONFIG_PATH
    existing: Config | None = None

    # --- Check existing config ---
    if config_path.exists():
        click.echo(f"\nConfig already exists at {config_path}")
        if not click.confirm("Reconfigure?", default=False):
            # Just run idempotent steps
            _register_uda_and_hooks()
            click.echo("\nRun `tickticksync daemon start` to begin syncing.")
            return
        # Load existing config to seed prompts
        try:
            existing = load_config(config_path)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            click.echo(f"Warning: could not parse existing config ({exc}); starting fresh.")
            existing = None

    # --- Step 1/4: Credentials ---
    click.echo("\n── Step 1/4: TickTick API credentials ──")
    client_id = click.prompt(
        "Client ID",
        default=existing.ticktick.client_id if existing else None,
    )
    if existing:
        client_secret = click.prompt(
            "Client secret (Enter to keep existing)",
            hide_input=True,
            default="",
            show_default=False,
        )
        if not client_secret:
            client_secret = existing.ticktick.client_secret
    else:
        client_secret = click.prompt("Client secret", hide_input=True)

    tt_cfg = TickTickConfig(client_id=client_id, client_secret=client_secret)
    auth_method = "oauth"
    tmp_cfg = Config(ticktick=tt_cfg, auth=AuthConfig(method=auth_method))

    # --- Step 2/4: OAuth ---
    click.echo("\n── Step 2/4: OAuth authentication ──")
    try:
        _run_oauth_flow(tt_cfg, tmp_cfg.token_path)
    except click.Abort:
        raise
    except Exception as exc:  # noqa: BLE001
        click.echo(f"OAuth authentication failed: {exc}")
        if not click.confirm("Skip auth for now?", default=True):
            raise click.ClickException(str(exc)) from exc
        click.echo(
            "You can authenticate later with: tickticksync auth oauth"
        )

    # --- Step 3/4: Sync settings ---
    click.echo("\n── Step 3/4: Sync settings ──")
    poll_interval = click.prompt(
        "Poll interval (seconds)",
        default=existing.sync.poll_interval if existing else SyncConfig.poll_interval,
        type=int,
    )
    socket_path = click.prompt(
        "Socket path",
        default=existing.sync.socket_path if existing else SyncConfig.socket_path,
    )

    # --- Step 4/4: Project mappings ---
    click.echo("\n── Step 4/4: Project mappings ──")

    # Seed from existing mappings
    projects: list[ProjectMapping] = list(existing.mapping.projects) if existing else []
    if projects:
        click.echo("Current mappings:")
        for pm in projects:
            click.echo(f"  {pm.ticktick} → {pm.taskwarrior}")

    # Try to fetch projects from TickTick
    tt_projects: list[dict] | None = None
    try:
        api = _build_api(tmp_cfg)
        tt_projects = _fetch_ticktick_projects(api)
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"Could not fetch TickTick projects: {exc}\n"
            "(If auth is not yet configured, this is expected.)"
        )
        click.echo("You can add mappings later with: tickticksync mapping add")

    if tt_projects is not None:
        mapped_names: set[str] = {p.ticktick for p in projects}
        mapped_tw_names: set[str] = {p.taskwarrior for p in projects}

        while True:
            # Show available (unmapped) projects
            unmapped = [p for p in tt_projects if p["name"] not in mapped_names]
            if not unmapped:
                click.echo("All projects have been mapped.")
                break

            if not click.confirm("Add a project mapping?", default=True):
                break

            for i, proj in enumerate(unmapped, 1):
                click.echo(f"  {i}. {proj['name']}")

            choice = click.prompt("Select project", type=int, default=1)
            if choice < 1 or choice > len(unmapped):
                click.echo(f"Invalid selection: {choice}")
                continue

            selected = unmapped[choice - 1]
            default_tw = selected["name"].lower()
            tw_name = click.prompt(
                "TaskWarrior project name", default=default_tw
            ).strip()

            if not tw_name:
                click.echo("TaskWarrior project name cannot be empty.")
                continue

            if tw_name in mapped_tw_names:
                click.echo(
                    f'TaskWarrior project "{tw_name}" is already used.'
                )
                continue

            projects.append(
                ProjectMapping(ticktick=selected["name"], taskwarrior=tw_name)
            )
            mapped_names.add(selected["name"])
            mapped_tw_names.add(tw_name)

    # --- Save config ---
    default_project = existing.mapping.default_project if existing else MappingConfig.default_project
    save_config_full(
        config_path,
        client_id=client_id,
        client_secret=client_secret,
        auth_method=auth_method,
        poll_interval=poll_interval,
        socket_path=socket_path,
        projects=projects,
        default_project=default_project,
    )
    click.echo(f"\n✓ Config saved to {config_path}")

    # --- UDA & hooks ---
    _register_uda_and_hooks()

    click.echo("\nRun `tickticksync daemon start` to begin syncing.")


@cli.command()
def sync() -> None:
    """Run one full sync cycle (no daemon required)."""
    cfg = load_config()
    if not cfg.mapping.projects:
        raise click.ClickException(
            "No project mappings configured. Run `tickticksync mapping add` first."
        )
    engine = _build_engine(cfg)

    async def _run() -> None:
        changes = await engine.run_cycle()
        click.echo(f"Detected {len(changes)} change(s).")
        click.echo("Sync complete.")

    asyncio.run(_run())


@cli.group()
def daemon() -> None:
    """Manage the sync daemon."""


@daemon.command("start")
def daemon_start() -> None:
    """Start the background sync daemon."""
    cfg = load_config()
    if not cfg.mapping.projects:
        raise click.ClickException(
            "No project mappings configured. Run `tickticksync mapping add` first."
        )
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)

    pid = os.fork()
    if pid > 0:
        PID_FILE.write_text(str(pid))
        click.echo(f"Daemon started (PID {pid})")
        return

    os.setsid()
    engine = _build_engine(cfg)
    queue: asyncio.Queue = asyncio.Queue()
    d = Daemon(
        sync_engine=engine,
        queue=queue,
        socket_path=cfg.sync.socket_path,
        queue_path=cfg.sync.queue_path,
        poll_interval=cfg.sync.poll_interval,
    )
    asyncio.run(d.run())


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the background sync daemon."""
    try:
        pid = int(PID_FILE.read_text())
    except (FileNotFoundError, ValueError):
        click.echo("Daemon is not running.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink()
        click.echo(f"Daemon (PID {pid}) stopped.")
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        click.echo("Daemon was not running (stale PID file removed).")


@daemon.command("status")
def daemon_status() -> None:
    """Show daemon running status."""
    pid = _read_pid()
    if pid is not None:
        click.echo(f"Daemon: running (PID {pid})")
    else:
        click.echo("Daemon: not running")


@cli.command()
def status() -> None:
    """Show sync status: mapped task count, last sync time."""
    cfg = load_config()
    state = StateStore(cfg.db_path)
    count = state.count_mappings()
    last_poll = state.get_state("last_poll_ts")
    last_poll_str = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last_poll)))
        if last_poll
        else "never"
    )
    click.echo(f"Mapped tasks : {count}")
    click.echo(f"Last sync    : {last_poll_str}")
    state.close()


# ---------------------------------------------------------------------------
# mapping group
# ---------------------------------------------------------------------------

@cli.group()
def mapping() -> None:
    """Manage TickTick-to-TaskWarrior project mappings."""


@mapping.command("list")
def mapping_list() -> None:
    """Display all configured project mappings."""
    cfg = load_config()
    projects = cfg.mapping.projects
    if not projects:
        click.echo("No project mappings configured.")
        click.echo("Run `tickticksync mapping add` to create one.")
        return
    tt_width = max(len(p.ticktick) for p in projects)
    tt_width = max(tt_width, len("TickTick Project"))
    tw_width = max(len(p.taskwarrior) for p in projects)
    tw_width = max(tw_width, len("TaskWarrior Project"))
    click.echo(f"{'TickTick Project':<{tt_width}}  → {'TaskWarrior Project':<{tw_width}}")
    click.echo("─" * (tt_width + 4 + tw_width))
    for pm in projects:
        click.echo(f"{pm.ticktick:<{tt_width}}  → {pm.taskwarrior:<{tw_width}}")
    click.echo(f"({len(projects)} mapping{'s' if len(projects) != 1 else ''})")


@mapping.command("remove")
@click.argument("ticktick_project")
def mapping_remove(ticktick_project: str) -> None:
    """Remove a project mapping by TickTick project name."""
    cfg = load_config()
    projects = cfg.mapping.projects
    updated = [p for p in projects if p.ticktick != ticktick_project]
    if len(updated) == len(projects):
        raise click.ClickException(f'No mapping found for "{ticktick_project}"')
    try:
        save_config_mapping(DEFAULT_CONFIG_PATH, updated)
    except OSError as err:
        raise click.ClickException(f"Failed to save config: {err}") from err
    click.echo(f'\u2713 Removed mapping for "{ticktick_project}"')


@mapping.command("add")
@click.option("--ticktick", default=None, help="TickTick project name")
@click.option("--taskwarrior", default=None, help="TaskWarrior project name")
def mapping_add(ticktick: str | None, taskwarrior: str | None) -> None:
    """Add a project mapping (interactive or via --ticktick/--taskwarrior)."""
    cfg = load_config()
    existing = cfg.mapping.projects
    mapped_names = {p.ticktick for p in existing}
    mapped_tw_names = {p.taskwarrior for p in existing}

    if bool(ticktick) != bool(taskwarrior):
        raise click.ClickException(
            "Both --ticktick and --taskwarrior are required together."
        )

    if ticktick and taskwarrior:
        if ticktick in mapped_names:
            raise click.ClickException(f'"{ticktick}" is already mapped.')
        if taskwarrior in mapped_tw_names:
            raise click.ClickException(
                f'TaskWarrior project "{taskwarrior}" is already used by another mapping.'
            )
        updated = [*existing, ProjectMapping(ticktick=ticktick, taskwarrior=taskwarrior)]
        try:
            save_config_mapping(DEFAULT_CONFIG_PATH, updated)
        except OSError as err:
            raise click.ClickException(f"Failed to save config: {err}") from err
        click.echo(f'\u2713 Mapped "{ticktick}" \u2192 "{taskwarrior}"')
        return

    # Interactive mode — fetch projects from TickTick API
    tt = _build_api(cfg)

    click.echo("Fetching TickTick projects...")
    try:
        all_projects = _fetch_ticktick_projects(tt)
    except Exception as exc:
        raise click.ClickException(f"Failed to fetch TickTick projects: {exc}") from exc
    unmapped = [p for p in all_projects if p["name"] not in mapped_names]

    if not unmapped:
        click.echo("All projects are already mapped.")
        return

    click.echo("\nUnmapped projects:")
    for i, proj in enumerate(unmapped, 1):
        click.echo(f"  {i}. {proj['name']}")

    choice = click.prompt("\nSelect project", type=int, default=1)
    if choice < 1 or choice > len(unmapped):
        raise click.ClickException(f"Invalid selection: {choice}")

    selected = unmapped[choice - 1]
    default_tw = selected["name"].lower()
    tw_name = click.prompt("TaskWarrior project name", default=default_tw).strip()

    if not tw_name:
        raise click.ClickException("TaskWarrior project name cannot be empty.")

    if tw_name in mapped_tw_names:
        raise click.ClickException(
            f'TaskWarrior project "{tw_name}" is already used by another mapping.'
        )

    updated = [*existing, ProjectMapping(ticktick=selected["name"], taskwarrior=tw_name)]
    try:
        save_config_mapping(DEFAULT_CONFIG_PATH, updated)
    except OSError as err:
        raise click.ClickException(f"Failed to save config: {err}") from err
    click.echo(f'\n\u2713 Mapped "{selected["name"]}" \u2192 "{tw_name}"')


# ---------------------------------------------------------------------------
# config group
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group() -> None:
    """View or modify configuration settings."""


_MASKED_FIELDS = frozenset({"client_secret"})

_SETTABLE_KEYS: dict[str, type] = {
    "sync.poll_interval": int,
    "sync.batch_window": int,
    "sync.socket_path": str,
    "sync.queue_path": str,
    "mapping.default_project": str,
}
if not all("." in k for k in _SETTABLE_KEYS):
    raise RuntimeError("All settable keys must be 'section.name'")


def _load_config_or_click() -> Config:
    """Load config, converting any failure into a clean ClickException."""
    try:
        return load_config()
    except FileNotFoundError:
        raise click.ClickException(
            "Config file not found. Run `tickticksync init` first."
        ) from None
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise click.ClickException(f"Failed to read config: {err}") from err


@config_group.command("show")
def config_show() -> None:
    """Pretty-print the current configuration."""
    cfg = _load_config_or_click()

    sections = [
        (f.name, getattr(cfg, f.name))
        for f in dataclasses.fields(cfg)
    ]
    for section_name, section_obj in sections:
        click.echo(f"\n[{section_name}]")
        for f in dataclasses.fields(section_obj):
            val = getattr(section_obj, f.name)
            if f.name in _MASKED_FIELDS:
                click.echo(f"  {f.name} = ****")
            elif f.name == "projects":
                if val:
                    click.echo(f"  {f.name} =")
                    for pm in val:
                        click.echo(f"    {pm.ticktick} -> {pm.taskwarrior}")
                else:
                    click.echo(f"  {f.name} = (none)")
            elif val is not None:
                click.echo(f"  {f.name} = {val}")
            else:
                click.echo(f"  {f.name} = (not set)")


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Update a configuration setting (e.g. sync.poll_interval 120)."""
    _load_config_or_click()  # validate config exists and is parseable

    if key not in _SETTABLE_KEYS:
        valid = ", ".join(sorted(_SETTABLE_KEYS))
        raise click.ClickException(f"Unknown key {key!r}. Valid keys: {valid}")

    section, name = key.split(".", 1)

    expected_type = _SETTABLE_KEYS[key]
    if expected_type is int:
        try:
            coerced_int: int = int(value)
        except ValueError:
            raise click.ClickException(f"{key!r} must be an integer, got {value!r}") from None
        if coerced_int <= 0:
            raise click.ClickException(f"{key!r} must be > 0, got {coerced_int}")
        coerced: object = coerced_int
    else:
        if not value.strip():
            raise click.ClickException(f"{key!r} must not be empty")
        coerced = value.strip()

    try:
        update_config_value(DEFAULT_CONFIG_PATH, section, name, coerced)
    except OSError as e:
        raise click.ClickException(f"Failed to save config: {e}") from e
    click.echo(f"{key} = {coerced}")
