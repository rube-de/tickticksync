import json
import socket
import sys
from pathlib import Path
from typing import Callable


def send_to_daemon(
    task: dict,
    socket_path: str,
    queue_path: str,
) -> None:
    """Send task JSON to daemon socket; fall back to queue file if unavailable."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(socket_path)
            sock.sendall(json.dumps(task).encode())
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        _append_to_queue(task, Path(queue_path))


def drain_queue(
    socket_path: str,
    queue_path: str,
    _send_fn: Callable = send_to_daemon,
) -> None:
    """Replay queued hook events. Called on daemon startup."""
    qp = Path(queue_path)
    if not qp.exists():
        return
    items: list[dict] = json.loads(qp.read_text())
    qp.unlink()
    for task in items:
        _send_fn(task, socket_path, queue_path)


def on_add_hook() -> None:
    """Entry point for TW on-add hook. Reads task from stdin, sends to daemon."""
    from tickticksync.config import load_config

    task = json.loads(sys.stdin.readline())
    cfg = load_config()
    send_to_daemon(task, cfg.sync.socket_path, cfg.sync.queue_path)
    print(json.dumps(task))  # TW hooks must echo the task back on stdout


def on_modify_hook() -> None:
    """Entry point for TW on-modify hook. Reads modified task from stdin."""
    from tickticksync.config import load_config

    _original = sys.stdin.readline()  # first line: original task (discard)
    modified = json.loads(sys.stdin.readline())
    cfg = load_config()
    send_to_daemon(modified, cfg.sync.socket_path, cfg.sync.queue_path)
    print(json.dumps(modified))


def _append_to_queue(task: dict, queue_path: Path) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = json.loads(queue_path.read_text()) if queue_path.exists() else []
    existing.append(task)
    queue_path.write_text(json.dumps(existing))
