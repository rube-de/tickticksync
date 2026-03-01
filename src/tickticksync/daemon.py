import asyncio
import json
import signal
import time
from pathlib import Path

from .hooks import drain_queue
from .sync import SyncEngine


async def handle_hook_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    queue: asyncio.Queue,
) -> None:
    try:
        data = await reader.read(65536)
        if data:
            task = json.loads(data.decode())
            await queue.put(task)
    finally:
        writer.close()


class Daemon:
    def __init__(
        self,
        sync_engine: SyncEngine,
        queue: asyncio.Queue,
        socket_path: str,
        queue_path: str,
        poll_interval: int = 60,
    ):
        self._engine = sync_engine
        self._queue = queue
        self._socket_path = socket_path
        self._queue_path = queue_path
        self._poll_interval = poll_interval
        self._running = False

    async def run(self) -> None:
        self._running = True
        drain_queue(self._socket_path, self._queue_path)
        socket_path = Path(self._socket_path)
        socket_path.unlink(missing_ok=True)

        server = await asyncio.start_unix_server(
            lambda r, w: handle_hook_connection(r, w, self._queue),
            path=str(socket_path),
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop)

        async with server:
            await asyncio.gather(
                self._hook_processor(),
                self._poll_loop(),
            )

    def _stop(self) -> None:
        self._running = False

    async def _hook_processor(self) -> None:
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._process_hook_task(task)
            except asyncio.TimeoutError:
                pass

    async def _poll_loop(self) -> None:
        while self._running:
            await self._run_sync_cycle()
            await asyncio.sleep(self._poll_interval)

    async def _flush_hook_queue(self) -> None:
        while not self._queue.empty():
            task = self._queue.get_nowait()
            await self._process_hook_task(task)

    async def _process_hook_task(self, task: dict) -> None:
        tw_tasks = [task]
        tt_tasks, project_map = await self._engine.tt.get_all_tasks()
        changes = self._engine.detect_changes(tw_tasks, tt_tasks)
        await self._engine.apply_changes(changes, project_map)

    async def _run_sync_cycle(self) -> None:
        tw_tasks = self._engine.tw.get_pending_tasks()
        tt_tasks, project_map = await self._engine.tt.get_all_tasks()
        changes = self._engine.detect_changes(tw_tasks, tt_tasks)
        await self._engine.apply_changes(changes, project_map)
        self._engine.store.set_state("last_poll_ts", str(time.time()))
