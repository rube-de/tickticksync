import asyncio
import os
import signal
import time
from pathlib import Path

import click

from .config import load_config
from .state import StateStore
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI
from .sync import SyncEngine
from .daemon import Daemon

PID_FILE = Path("~/.local/share/tickticksync/tickticksync.pid").expanduser()
HOOKS_DIR = Path("~/.local/share/task/hooks").expanduser()


@click.group()
def cli() -> None:
    """TickTick <-> TaskWarrior bidirectional sync."""


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
    state = StateStore(cfg.db_path)
    tw = TaskWarriorClient()
    tt = TickTickAPI(
        cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path)
    )
    engine = SyncEngine(store=state, tw=tw, tt=tt)

    async def _run() -> None:
        tw_tasks = tw.get_pending_tasks()
        tt_tasks, project_map = await tt.get_all_tasks()
        changes = engine.detect_changes(tw_tasks, tt_tasks)
        click.echo(f"Detected {len(changes)} change(s).")
        await engine.apply_changes(changes, project_map)
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
    state = StateStore(cfg.db_path)
    tw = TaskWarriorClient()
    tt = TickTickAPI(
        cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path)
    )
    engine = SyncEngine(store=state, tw=tw, tt=tt)
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
    if not PID_FILE.exists():
        click.echo("Daemon is not running.")
        return
    pid = int(PID_FILE.read_text())
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
    if not PID_FILE.exists():
        click.echo("Daemon: not running")
        return
    pid = int(PID_FILE.read_text())
    try:
        os.kill(pid, 0)
        click.echo(f"Daemon: running (PID {pid})")
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        click.echo("Daemon: not running (stale PID file removed)")


@cli.command()
def status() -> None:
    """Show sync status: mapped task count, last sync time."""
    cfg = load_config()
    state = StateStore(cfg.db_path)
    count = len(state.all_mappings())
    last_poll = state.get_state("last_poll_ts")
    last_poll_str = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last_poll)))
        if last_poll
        else "never"
    )
    click.echo(f"Mapped tasks : {count}")
    click.echo(f"Last sync    : {last_poll_str}")
    state.close()
