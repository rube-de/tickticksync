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

from .config import AuthConfig, Config, load_config, save_config_auth
from .state import StateStore
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI
from .sync import SyncEngine
from .daemon import Daemon

PID_FILE = Path("~/.local/share/tickticksync/tickticksync.pid").expanduser()
HOOKS_DIR = Path("~/.local/share/task/hooks").expanduser()


_KEYRING_SERVICE = "tickticksync"


def _build_engine(cfg: Config) -> SyncEngine:
    state = StateStore(cfg.db_path)
    tw = TaskWarriorClient()

    if cfg.auth.method == "password":
        username = cfg.auth.username
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
        tt = TickTickAPI(
            cfg.ticktick.client_id,
            cfg.ticktick.client_secret,
            str(cfg.token_path),
            username=username,
            password=password,
        )
    else:
        tt = TickTickAPI(
            cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path)
        )

    return SyncEngine(
        store=state, tw=tw, tt=tt, default_project=cfg.mapping.default_project
    )


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
    config_path = Path("~/.config/tickticksync/config.toml").expanduser()
    cfg = load_config(config_path)

    redirect_uri = "http://localhost:8080/callback"
    handler = OAuth2Handler(cfg.ticktick.client_id, cfg.ticktick.client_secret, redirect_uri)
    auth_url, state = handler.get_authorization_url()

    click.echo(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Capture the callback on localhost:8080
    captured: dict[str, str] = {}
    done = threading.Event()

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            captured["code"] = params.get("code", [""])[0]
            captured["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Auth complete. You can close this tab.")
            done.set()

        def log_message(self, *_: object) -> None:  # suppress access logs
            pass

    server = http.server.HTTPServer(("localhost", 8080), _CallbackHandler)
    click.echo("Waiting for OAuth callback on http://localhost:8080 …")
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    done.wait(timeout=300)
    server.shutdown()

    if not captured.get("code"):
        raise click.ClickException("No OAuth code received within 5 minutes.")

    token = asyncio.run(handler.exchange_code(captured["code"], captured["state"]))

    token_path = cfg.token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({"access_token": token.access_token}))
    click.echo(f"Token saved to {token_path}")

    save_config_auth(config_path, "oauth")
    click.echo("Auth method set to 'oauth' in config.")


@auth.command("password")
def auth_password() -> None:
    """Store TickTick username/password credentials in the system keyring."""
    config_path = Path("~/.config/tickticksync/config.toml").expanduser()
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
    config_dir = Path("~/.config/tickticksync").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"

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
