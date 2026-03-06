import json
import socket
from pathlib import Path
from tickticksync.hooks import send_to_daemon, drain_queue


SOCKET_PATH = "/tmp/test_tickticksync.sock"
TASK_JSON = {"uuid": "uuid-1", "description": "Test task"}


def test_send_to_daemon_writes_to_socket(tmp_path):
    queue_path = tmp_path / "queue.json"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)
    server.settimeout(1)
    try:
        send_to_daemon(TASK_JSON, SOCKET_PATH, str(queue_path))
        conn, _ = server.accept()
        data = conn.recv(4096)
        conn.close()
        assert json.loads(data) == TASK_JSON
    finally:
        server.close()
        Path(SOCKET_PATH).unlink(missing_ok=True)


def test_send_to_daemon_falls_back_to_queue_when_no_socket(tmp_path):
    queue_path = tmp_path / "queue.json"
    send_to_daemon(TASK_JSON, "/tmp/no_such_socket.sock", str(queue_path))
    items = json.loads(queue_path.read_text())
    assert items[0] == TASK_JSON


def test_send_to_daemon_appends_to_existing_queue(tmp_path):
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps([{"uuid": "existing"}]))
    send_to_daemon(TASK_JSON, "/tmp/no_such_socket.sock", str(queue_path))
    items = json.loads(queue_path.read_text())
    assert len(items) == 2


def test_drain_queue_sends_all_items_and_clears_file(tmp_path):
    queue_path = tmp_path / "queue.json"
    items = [{"uuid": "a"}, {"uuid": "b"}]
    queue_path.write_text(json.dumps(items))
    sent: list[dict] = []

    def fake_send(task, socket_path, qp):
        sent.append(task)

    drain_queue(SOCKET_PATH, str(queue_path), _send_fn=fake_send)
    assert len(sent) == 2
    assert not queue_path.exists()


def test_drain_queue_noop_when_no_file(tmp_path):
    queue_path = tmp_path / "queue.json"
    drain_queue(SOCKET_PATH, str(queue_path))  # must not raise
