import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from tickticksync.daemon import Daemon, handle_hook_connection


@pytest.mark.asyncio
async def test_handle_hook_connection_enqueues_task():
    queue: asyncio.Queue = asyncio.Queue()
    reader = AsyncMock()
    reader.read.return_value = json.dumps({"uuid": "u1"}).encode()
    writer = MagicMock()
    await handle_hook_connection(reader, writer, queue)
    assert not queue.empty()
    task = await queue.get()
    assert task["uuid"] == "u1"


@pytest.mark.asyncio
async def test_daemon_processes_queue_item(tmp_path):
    sync_engine = MagicMock()
    sync_engine.tt = AsyncMock()
    sync_engine.tt.get_all_tasks.return_value = ([], {})
    sync_engine.detect_changes.return_value = []
    sync_engine.apply_changes = AsyncMock()
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({"uuid": "u1", "description": "Task"})

    daemon = Daemon(
        sync_engine=sync_engine,
        queue=queue,
        socket_path=str(tmp_path / "test.sock"),
        queue_path=str(tmp_path / "queue.json"),
        poll_interval=9999,
    )
    await daemon._flush_hook_queue()
    assert queue.empty()
