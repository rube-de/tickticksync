import fcntl
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
    try:
        items: list[dict] = json.loads(qp.read_text())
        qp.unlink()
    except FileNotFoundError:
        return
    for task in items:
        _send_fn(task, socket_path, queue_path)


def _run_hook(skip_lines: int = 0) -> None:
    """Shared logic for TW hook entry points."""
    from tickticksync.config import load_config

    for _ in range(skip_lines):
        sys.stdin.readline()
    task = json.loads(sys.stdin.readline())
    cfg = load_config()
    send_to_daemon(task, cfg.sync.socket_path, cfg.sync.queue_path)
    print(json.dumps(task))


def on_add_hook() -> None:
    """Entry point for TW on-add hook. Reads task from stdin, sends to daemon."""
    _run_hook(skip_lines=0)


def on_modify_hook() -> None:
    """Entry point for TW on-modify hook. Reads modified task from stdin."""
    _run_hook(skip_lines=1)


def _append_to_queue(task: dict, queue_path: Path) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(queue_path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        content = fd.read()
        existing: list[dict] = json.loads(content) if content else []
        existing.append(task)
        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(existing))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
