import asyncio
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

from .config import DEFAULT_CONFIG_PATH, Config, ProjectMapping, load_config, save_config_auth, save_config_mapping
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

    api_args = (cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path))

    if cfg.auth.method == "password":
        username = cfg.auth.username
        if not username:
            raise click.ClickException(
                "No username configured. Run `tickticksync auth password`."
            )
        try:
            password = keyring.get_password(_KEYRING_SERVICE, username)
        except keyring.errors.NoKeyringError:
            raise click.ClickException(
                "No system keyring available. Run `tickticksync auth password` on a"
                " machine with a keyring (macOS Keychain or Linux Secret Service)."
            )
        if not password:
            raise click.ClickException(
                f"No stored password for {username!r}. Run `tickticksync auth password`."
            )
        tt = TickTickAPI(*api_args, username=username, password=password, use_v2_tasks=True)
    else:
        tt = TickTickAPI(*api_args)

    return SyncEngine(
        store=state, tw=tw, tt=tt, default_project=cfg.mapping.default_project
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
        except keyring.errors.NoKeyringError:
            raise click.ClickException(
                "No system keyring available. Run `tickticksync auth password` on a"
                " machine with a keyring (macOS Keychain or Linux Secret Service)."
            )
        if not password:
            raise click.ClickException(
                f"No stored password for {username!r}. Run `tickticksync auth password`."
            )
        return TickTickAPI(*api_args, username=username, password=password, use_v2_tasks=True)
    return TickTickAPI(*api_args)


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


@auth.command("oauth")
def auth_oauth() -> None:
    """Run the browser OAuth2 flow and save the access token."""
    config_path = DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)

    parsed_redirect = urlparse(_OAUTH_REDIRECT_URI)
    handler = OAuth2Handler(cfg.ticktick.client_id, cfg.ticktick.client_secret, _OAUTH_REDIRECT_URI)
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

    token_path = cfg.token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({"access_token": token.access_token}))
    token_path.chmod(0o600)
    click.echo(f"Token saved to {token_path}")

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

@cli.command()
def init() -> None:
    """Set up OAuth credentials, register TW UDA, and install hooks."""
    config_path = DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if not config_path.exists():
        client_id = click.prompt("TickTick OAuth client_id")
        client_secret = click.prompt("TickTick OAuth client_secret", hide_input=True)
        config_path.write_text(
            f'[ticktick]\nclient_id = "{client_id}"\nclient_secret = "{client_secret}"\n'
        )
        click.echo(f"Config written to {config_path}")

    load_config(config_path)

    tw = TaskWarriorClient()
    tw.register_uda("ticktickid", "string", "TickTick ID")
    click.echo("Registered TW UDA: ticktickid")

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
    click.echo(f"Hook scripts installed in {HOOKS_DIR}")
    click.echo("\nRun `tickticksync daemon start` to begin syncing.")


@cli.command()
def sync() -> None:
    """Run one full sync cycle (no daemon required)."""
    cfg = load_config()
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
    click.echo("TickTick Project    → TaskWarrior Project")
    click.echo("─" * 41)
    for pm in projects:
        click.echo(f"{pm.ticktick:<20}→ {pm.taskwarrior}")
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
    save_config_mapping(DEFAULT_CONFIG_PATH, updated)
    click.echo(f'\u2713 Removed mapping for "{ticktick_project}"')


@mapping.command("add")
@click.option("--ticktick", default=None, help="TickTick project name")
@click.option("--taskwarrior", default=None, help="TaskWarrior project name")
def mapping_add(ticktick: str | None, taskwarrior: str | None) -> None:
    """Add a project mapping (interactive or via --ticktick/--taskwarrior)."""
    cfg = load_config()
    existing = cfg.mapping.projects
    mapped_names = {p.ticktick for p in existing}

    if bool(ticktick) != bool(taskwarrior):
        raise click.ClickException(
            "Both --ticktick and --taskwarrior are required together."
        )

    if ticktick and taskwarrior:
        if ticktick in mapped_names:
            raise click.ClickException(f'"{ticktick}" is already mapped.')
        updated = list(existing) + [ProjectMapping(ticktick=ticktick, taskwarrior=taskwarrior)]
        save_config_mapping(DEFAULT_CONFIG_PATH, updated)
        click.echo(f'\u2713 Mapped "{ticktick}" \u2192 "{taskwarrior}"')
        return

    # Interactive mode — fetch projects from TickTick API
    tt = _build_api(cfg)

    async def _fetch() -> list[dict]:
        await tt.connect()
        try:
            return await tt.get_projects()
        finally:
            await tt.disconnect()

    click.echo("Fetching TickTick projects...")
    all_projects = asyncio.run(_fetch())
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
    tw_name = click.prompt("TaskWarrior project name", default=default_tw)

    updated = list(existing) + [ProjectMapping(ticktick=selected["name"], taskwarrior=tw_name)]
    save_config_mapping(DEFAULT_CONFIG_PATH, updated)
    click.echo(f'\n\u2713 Mapped "{selected["name"]}" \u2192 "{tw_name}"')
