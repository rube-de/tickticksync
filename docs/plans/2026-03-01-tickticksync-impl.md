# tickticksync Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a bidirectional TickTick ↔ TaskWarrior sync daemon in Python.

**Architecture:** A background daemon listens on a Unix socket for TW hook events (push path) and polls the TickTick V1 API on a timer (pull path). SQLite tracks task mappings and last-known state for last-write-wins conflict resolution.

**Tech Stack:** Python 3.11+, uv, ticktick-sdk (dev-mirzabicer), tasklib, click, sqlite3 (stdlib), asyncio

---

## Pre-flight: Verify the TickTick SDK

Before starting, check the actual PyPI name and import path:
```bash
uv run python -c "import ticktick_sdk; print(ticktick_sdk.__version__)"
# If that fails, try: pip show ticktick-sdk  OR  search PyPI for "ticktick"
```
The research identified `dev-mirzabicer/ticktick-sdk` as the target. Verify at https://pypi.org/project/ticktick-sdk/ — adapt imports in Tasks 6+ if the package name differs.

---

## Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/tickticksync/__init__.py`
- Create: `tests/conftest.py`

**Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tickticksync"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "ticktick-sdk",
    "tasklib",
    "click>=8.0",
]

[project.scripts]
tickticksync = "tickticksync.cli:cli"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio",
    "respx",
]
```

**Step 2: Create `src/tickticksync/__init__.py`**

```python
__version__ = "0.1.0"
```

**Step 3: Create `tests/conftest.py`**

```python
import pytest
from pathlib import Path


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[ticktick]
client_id = "test_id"
client_secret = "test_secret"
""")
    return cfg
```

**Step 4: Install dependencies**

```bash
uv sync --dev
```

**Step 5: Verify imports resolve**

```bash
uv run python -c "import click, tasklib; print('OK')"
```

Expected: `OK`

**Step 6: Commit**

```bash
git add pyproject.toml src/ tests/
git commit -m "chore: scaffold project structure"
```

---

## Task 2: Config Module

**Files:**
- Create: `src/tickticksync/config.py`
- Create: `tests/test_config.py`

**Step 1: Write failing tests**

```python
# tests/test_config.py
import pytest
from pathlib import Path
from tickticksync.config import load_config, Config, SyncConfig, MappingConfig


def test_load_minimal_config(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.ticktick.client_id == "test_id"
    assert cfg.ticktick.client_secret == "test_secret"


def test_sync_defaults(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.sync.poll_interval == 60
    assert cfg.sync.batch_window == 5
    assert cfg.sync.socket_path == "/tmp/tickticksync.sock"


def test_mapping_defaults(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.mapping.default_project == "inbox"


def test_missing_client_id_raises(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[ticktick]\nclient_secret = 'x'\n")
    with pytest.raises((KeyError, TypeError)):
        load_config(bad)


def test_custom_poll_interval(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[ticktick]
client_id = "id"
client_secret = "secret"
[sync]
poll_interval = 30
""")
    cfg = load_config(cfg_path)
    assert cfg.sync.poll_interval == 30
```

**Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'tickticksync.config'`

**Step 3: Implement `src/tickticksync/config.py`**

```python
from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass
class TickTickConfig:
    client_id: str
    client_secret: str


@dataclass
class SyncConfig:
    poll_interval: int = 60
    batch_window: int = 5
    socket_path: str = "/tmp/tickticksync.sock"
    queue_path: str = "~/.local/share/tickticksync/hook_queue.json"


@dataclass
class MappingConfig:
    default_project: str = "inbox"


@dataclass
class Config:
    ticktick: TickTickConfig
    sync: SyncConfig = field(default_factory=SyncConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)

    @property
    def token_path(self) -> Path:
        return Path("~/.config/tickticksync/token.json").expanduser()

    @property
    def db_path(self) -> Path:
        return Path("~/.local/share/tickticksync/state.db").expanduser()


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = Path("~/.config/tickticksync/config.toml").expanduser()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config(
        ticktick=TickTickConfig(**data["ticktick"]),
        sync=SyncConfig(**data.get("sync", {})),
        mapping=MappingConfig(**data.get("mapping", {})),
    )
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all 5 tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/config.py tests/test_config.py
git commit -m "feat: add config module with TOML loading"
```

---

## Task 3: State Store

**Files:**
- Create: `src/tickticksync/state.py`
- Create: `tests/test_state.py`

**Step 1: Write failing tests**

```python
# tests/test_state.py
import pytest
import time
from tickticksync.state import StateStore, TaskMapping


@pytest.fixture
def store(tmp_db):
    s = StateStore(tmp_db)
    yield s
    s.close()


def make_mapping(**kwargs) -> TaskMapping:
    defaults = dict(
        tw_uuid="uuid-1",
        ticktick_id="tt-1",
        ticktick_project="proj-1",
        last_sync_ts=time.time(),
        tw_modified="2024-01-01T10:00:00Z",
        ticktick_modified="2024-01-01T10:00:00Z",
    )
    return TaskMapping(**{**defaults, **kwargs})


def test_upsert_and_get_by_tw_uuid(store):
    m = make_mapping()
    store.upsert_mapping(m)
    result = store.get_by_tw_uuid("uuid-1")
    assert result.ticktick_id == "tt-1"


def test_upsert_and_get_by_ticktick_id(store):
    store.upsert_mapping(make_mapping())
    result = store.get_by_ticktick_id("tt-1")
    assert result.tw_uuid == "uuid-1"


def test_upsert_is_idempotent(store):
    store.upsert_mapping(make_mapping(tw_modified="2024-01-01"))
    store.upsert_mapping(make_mapping(tw_modified="2024-06-01"))
    result = store.get_by_tw_uuid("uuid-1")
    assert result.tw_modified == "2024-06-01"


def test_get_missing_returns_none(store):
    assert store.get_by_tw_uuid("no-such") is None
    assert store.get_by_ticktick_id("no-such") is None


def test_all_mappings(store):
    store.upsert_mapping(make_mapping(tw_uuid="a", ticktick_id="ta"))
    store.upsert_mapping(make_mapping(tw_uuid="b", ticktick_id="tb"))
    assert len(store.all_mappings()) == 2


def test_delete_by_tw_uuid(store):
    store.upsert_mapping(make_mapping())
    store.delete_by_tw_uuid("uuid-1")
    assert store.get_by_tw_uuid("uuid-1") is None


def test_get_set_state(store):
    assert store.get_state("last_poll") is None
    store.set_state("last_poll", "2024-01-01T00:00:00Z")
    assert store.get_state("last_poll") == "2024-01-01T00:00:00Z"


def test_set_state_overwrites(store):
    store.set_state("key", "v1")
    store.set_state("key", "v2")
    assert store.get_state("key") == "v2"
```

**Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_state.py -v
```

Expected: `ModuleNotFoundError`

**Step 3: Implement `src/tickticksync/state.py`**

```python
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TaskMapping:
    tw_uuid: str
    ticktick_id: str
    ticktick_project: str
    last_sync_ts: float
    tw_modified: Optional[str] = None
    ticktick_modified: Optional[str] = None


class StateStore:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_map (
                tw_uuid           TEXT PRIMARY KEY,
                ticktick_id       TEXT UNIQUE NOT NULL,
                ticktick_project  TEXT NOT NULL,
                last_sync_ts      REAL NOT NULL,
                tw_modified       TEXT,
                ticktick_modified TEXT
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self._conn.commit()

    def upsert_mapping(self, m: TaskMapping) -> None:
        self._conn.execute(
            """
            INSERT INTO task_map VALUES (?,?,?,?,?,?)
            ON CONFLICT(tw_uuid) DO UPDATE SET
                ticktick_id       = excluded.ticktick_id,
                ticktick_project  = excluded.ticktick_project,
                last_sync_ts      = excluded.last_sync_ts,
                tw_modified       = excluded.tw_modified,
                ticktick_modified = excluded.ticktick_modified
            """,
            (m.tw_uuid, m.ticktick_id, m.ticktick_project,
             m.last_sync_ts, m.tw_modified, m.ticktick_modified),
        )
        self._conn.commit()

    def get_by_tw_uuid(self, tw_uuid: str) -> Optional[TaskMapping]:
        row = self._conn.execute(
            "SELECT * FROM task_map WHERE tw_uuid=?", (tw_uuid,)
        ).fetchone()
        return TaskMapping(*row) if row else None

    def get_by_ticktick_id(self, ticktick_id: str) -> Optional[TaskMapping]:
        row = self._conn.execute(
            "SELECT * FROM task_map WHERE ticktick_id=?", (ticktick_id,)
        ).fetchone()
        return TaskMapping(*row) if row else None

    def all_mappings(self) -> list[TaskMapping]:
        rows = self._conn.execute("SELECT * FROM task_map").fetchall()
        return [TaskMapping(*r) for r in rows]

    def delete_by_tw_uuid(self, tw_uuid: str) -> None:
        self._conn.execute("DELETE FROM task_map WHERE tw_uuid=?", (tw_uuid,))
        self._conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO sync_state VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_state.py -v
```

Expected: all 9 tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/state.py tests/test_state.py
git commit -m "feat: add SQLite state store"
```

---

## Task 4: Field Mapper

**Files:**
- Create: `src/tickticksync/mapper.py`
- Create: `tests/test_mapper.py`

**Step 1: Write failing tests**

```python
# tests/test_mapper.py
from tickticksync.mapper import (
    tw_task_to_ticktick,
    ticktick_task_to_tw,
    TW_TO_TT_PRIORITY,
    TT_TO_TW_PRIORITY,
)


def test_priority_maps_are_inverses():
    for tw_p, tt_p in TW_TO_TT_PRIORITY.items():
        assert TT_TO_TW_PRIORITY[tt_p] == tw_p


def test_tw_to_tt_basic():
    tw = {"description": "Buy milk", "priority": "H", "status": "pending"}
    result = tw_task_to_ticktick(tw, project_id="proj-1")
    assert result["title"] == "Buy milk"
    assert result["priority"] == 5
    assert result["status"] == 0
    assert result["projectId"] == "proj-1"


def test_tw_to_tt_completed():
    tw = {"description": "Done", "status": "completed"}
    result = tw_task_to_ticktick(tw, project_id="p")
    assert result["status"] == 2


def test_tw_to_tt_no_priority():
    tw = {"description": "Task", "status": "pending"}
    result = tw_task_to_ticktick(tw, project_id="p")
    assert result["priority"] == 0


def test_tw_to_tt_due_date():
    tw = {"description": "Task", "status": "pending", "due": "2024-06-01T12:00:00Z"}
    result = tw_task_to_ticktick(tw, project_id="p")
    assert result["dueDate"] == "2024-06-01T12:00:00Z"


def test_tw_to_tt_annotations_become_content():
    tw = {
        "description": "Task",
        "status": "pending",
        "annotations": [{"description": "Note one"}, {"description": "Note two"}],
    }
    result = tw_task_to_ticktick(tw, project_id="p")
    assert "Note one" in result["content"]
    assert "Note two" in result["content"]


def test_tt_to_tw_basic():
    tt = {"id": "tt-1", "title": "Buy milk", "priority": 5, "status": 0}
    result = ticktick_task_to_tw(tt, project_name="Personal")
    assert result["description"] == "Buy milk"
    assert result["priority"] == "H"
    assert result["status"] == "pending"
    assert result["project"] == "Personal"


def test_tt_to_tw_completed():
    tt = {"id": "tt-1", "title": "Done", "status": 2}
    result = ticktick_task_to_tw(tt, project_name="P")
    assert result["status"] == "completed"


def test_tt_to_tw_subtask_items_become_annotations():
    tt = {
        "id": "tt-1",
        "title": "Task",
        "status": 0,
        "items": [
            {"title": "Step 1", "status": 2},
            {"title": "Step 2", "status": 0},
        ],
    }
    result = ticktick_task_to_tw(tt, project_name="P")
    ann_texts = [a["description"] for a in result.get("annotations", [])]
    assert "[x] Step 1" in ann_texts
    assert "[ ] Step 2" in ann_texts


def test_tt_to_tw_content_becomes_annotation():
    tt = {"id": "tt-1", "title": "Task", "status": 0, "content": "Some notes"}
    result = ticktick_task_to_tw(tt, project_name="P")
    ann_texts = [a["description"] for a in result.get("annotations", [])]
    assert "Some notes" in ann_texts
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_mapper.py -v
```

**Step 3: Implement `src/tickticksync/mapper.py`**

```python
from typing import Optional

TW_TO_TT_PRIORITY: dict[Optional[str], int] = {"H": 5, "M": 3, "L": 1, None: 0}
TT_TO_TW_PRIORITY: dict[int, Optional[str]] = {5: "H", 3: "M", 1: "L", 0: None}


def tw_task_to_ticktick(tw_task: dict, project_id: str) -> dict:
    """Convert a TaskWarrior task dict to a TickTick task payload."""
    tt: dict = {
        "title": tw_task["description"],
        "projectId": project_id,
        "priority": TW_TO_TT_PRIORITY.get(tw_task.get("priority"), 0),
        "status": 2 if tw_task.get("status") == "completed" else 0,
    }
    if due := tw_task.get("due"):
        tt["dueDate"] = due
    annotations = tw_task.get("annotations", [])
    # exclude subtask-style annotations (already have [x]/[ ] prefix) from content
    content_parts = [
        a["description"]
        for a in annotations
        if not a["description"].startswith("[")
    ]
    if content_parts:
        tt["content"] = "\n".join(content_parts)
    return tt


def ticktick_task_to_tw(tt_task: dict, project_name: str) -> dict:
    """Convert a TickTick task dict to a TaskWarrior task dict."""
    tw: dict = {
        "description": tt_task["title"],
        "project": project_name,
        "priority": TT_TO_TW_PRIORITY.get(tt_task.get("priority", 0)),
        "status": "completed" if tt_task.get("status") == 2 else "pending",
    }
    if due := tt_task.get("dueDate"):
        tw["due"] = due

    annotations: list[dict] = []
    if content := tt_task.get("content"):
        annotations.append({"description": content})
    for item in tt_task.get("items", []):
        prefix = "[x]" if item.get("status") == 2 else "[ ]"
        annotations.append({"description": f"{prefix} {item['title']}"})
    if annotations:
        tw["annotations"] = annotations

    return tw
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_mapper.py -v
```

Expected: all 11 tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/mapper.py tests/test_mapper.py
git commit -m "feat: add bidirectional field mapper"
```

---

## Task 5: TaskWarrior Wrapper

**Files:**
- Create: `src/tickticksync/taskwarrior.py`
- Create: `tests/test_taskwarrior.py`

**Step 1: Write failing tests**

```python
# tests/test_taskwarrior.py
import pytest
from unittest.mock import MagicMock, patch, call
from tickticksync.taskwarrior import TaskWarriorClient


@pytest.fixture
def mock_tw():
    with patch("tickticksync.taskwarrior.tasklib") as mock_lib:
        mock_warrior = MagicMock()
        mock_lib.TaskWarrior.return_value = mock_warrior
        mock_lib.Task = MagicMock
        yield mock_warrior, mock_lib


def test_get_pending_tasks_calls_filter(mock_tw):
    warrior, lib = mock_tw
    client = TaskWarriorClient()
    warrior.tasks.filter.return_value = []
    result = client.get_pending_tasks()
    warrior.tasks.filter.assert_called_once_with(status="pending")
    assert result == []


def test_create_task_saves_and_returns_uuid(mock_tw):
    warrior, lib = mock_tw
    mock_task = MagicMock()
    mock_task.__getitem__ = lambda self, k: "uuid-abc" if k == "uuid" else None
    lib.Task.return_value = mock_task
    client = TaskWarriorClient()
    uuid = client.create_task({"description": "Test"})
    mock_task.save.assert_called_once()
    assert uuid == "uuid-abc"


def test_update_task_raises_on_missing(mock_tw):
    warrior, _ = mock_tw
    warrior.tasks.filter.return_value = []
    client = TaskWarriorClient()
    with pytest.raises(ValueError, match="not found"):
        client.update_task("no-such-uuid", {"description": "x"})


def test_complete_task_calls_done(mock_tw):
    warrior, _ = mock_tw
    mock_task = MagicMock()
    warrior.tasks.filter.return_value = [mock_task]
    client = TaskWarriorClient()
    client.complete_task("uuid-1")
    mock_task.done.assert_called_once()


def test_delete_task_calls_delete(mock_tw):
    warrior, _ = mock_tw
    mock_task = MagicMock()
    warrior.tasks.filter.return_value = [mock_task]
    client = TaskWarriorClient()
    client.delete_task("uuid-1")
    mock_task.delete.assert_called_once()
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_taskwarrior.py -v
```

**Step 3: Implement `src/tickticksync/taskwarrior.py`**

```python
import subprocess
from typing import Optional
import tasklib


class TaskWarriorClient:
    def __init__(self, data_location: Optional[str] = None):
        kwargs: dict = {}
        if data_location:
            kwargs["data_location"] = data_location
        self._tw = tasklib.TaskWarrior(**kwargs)

    def get_pending_tasks(self) -> list[dict]:
        return [self._task_to_dict(t) for t in self._tw.tasks.filter(status="pending")]

    def get_task_by_uuid(self, uuid: str) -> Optional[dict]:
        tasks = self._tw.tasks.filter(uuid=uuid)
        return self._task_to_dict(tasks[0]) if tasks else None

    def create_task(self, fields: dict) -> str:
        task = tasklib.Task(self._tw, **fields)
        task.save()
        return str(task["uuid"])

    def update_task(self, uuid: str, fields: dict) -> None:
        tasks = self._tw.tasks.filter(uuid=uuid)
        if not tasks:
            raise ValueError(f"Task {uuid} not found")
        task = tasks[0]
        for k, v in fields.items():
            task[k] = v
        task.save()

    def complete_task(self, uuid: str) -> None:
        tasks = self._tw.tasks.filter(uuid=uuid)
        if tasks:
            tasks[0].done()

    def delete_task(self, uuid: str) -> None:
        tasks = self._tw.tasks.filter(uuid=uuid)
        if tasks:
            tasks[0].delete()

    def register_uda(self, name: str, type_: str, label: str) -> None:
        subprocess.run(["task", "config", f"uda.{name}.type", type_], check=True)
        subprocess.run(["task", "config", f"uda.{name}.label", label], check=True)

    @staticmethod
    def _task_to_dict(task: tasklib.Task) -> dict:
        return dict(task)
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_taskwarrior.py -v
```

Expected: all 5 tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/taskwarrior.py tests/test_taskwarrior.py
git commit -m "feat: add TaskWarrior wrapper"
```

---

## Task 6: TickTick Client

> **Note:** Verify the actual `ticktick-sdk` import path and client constructor before writing the implementation. Run `uv run python -c "import ticktick_sdk; help(ticktick_sdk)"` to inspect the API. The wrapper below shows the interface we need — adapt the internal SDK calls to match.

**Files:**
- Create: `src/tickticksync/ticktick.py`
- Create: `tests/test_ticktick.py`

**Step 1: Inspect the SDK**

```bash
uv run python -c "from ticktick_sdk import TickTickClient; help(TickTickClient)"
```

Note the exact constructor signature and method names. Update the implementation in Step 3 accordingly.

**Step 2: Write failing tests**

```python
# tests/test_ticktick.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tickticksync.ticktick import TickTickAPI


@pytest.fixture
def mock_sdk():
    with patch("tickticksync.ticktick.TickTickClient") as MockClient:
        mock_client = AsyncMock()
        MockClient.return_value = mock_client
        yield mock_client


@pytest.mark.asyncio
async def test_get_projects(mock_sdk):
    mock_sdk.get_projects.return_value = [{"id": "p1", "name": "Inbox"}]
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    result = await api.get_projects()
    assert result[0]["id"] == "p1"


@pytest.mark.asyncio
async def test_get_all_tasks_aggregates_projects(mock_sdk):
    mock_sdk.get_projects.return_value = [{"id": "p1", "name": "Work"}]
    mock_sdk.get_project_data.return_value = {
        "tasks": [{"id": "t1", "title": "Task 1", "deleted": 0}]
    }
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    tasks, project_map = await api.get_all_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t1"
    assert project_map["p1"] == "Work"


@pytest.mark.asyncio
async def test_get_all_tasks_filters_deleted(mock_sdk):
    mock_sdk.get_projects.return_value = [{"id": "p1", "name": "Work"}]
    mock_sdk.get_project_data.return_value = {
        "tasks": [
            {"id": "t1", "title": "Active", "deleted": 0},
            {"id": "t2", "title": "Deleted", "deleted": 1},
        ]
    }
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    tasks, _ = await api.get_all_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t1"


@pytest.mark.asyncio
async def test_create_task(mock_sdk):
    mock_sdk.create_task.return_value = {"id": "new-1", "title": "Buy milk"}
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    result = await api.create_task({"title": "Buy milk", "projectId": "p1"})
    assert result["id"] == "new-1"
    mock_sdk.create_task.assert_called_once()
```

**Step 3: Run to confirm failure**

```bash
uv run pytest tests/test_ticktick.py -v
```

**Step 4: Implement `src/tickticksync/ticktick.py`**

```python
# Adapt the internal SDK calls based on your inspection in Step 1.
# The public interface of TickTickAPI must match exactly what tests expect.
from typing import Any
# from ticktick_sdk import TickTickClient  # <- verify exact import


class TickTickAPI:
    def __init__(self, client_id: str, client_secret: str, token_path: str):
        # Adapt constructor to match SDK signature
        self._client = TickTickClient(
            client_id=client_id,
            client_secret=client_secret,
            token_path=token_path,
        )

    async def get_projects(self) -> list[dict]:
        return await self._client.get_projects()

    async def get_all_tasks(self) -> tuple[list[dict], dict[str, str]]:
        """Returns (tasks, project_map) where project_map is {id: name}."""
        projects = await self.get_projects()
        project_map = {p["id"]: p["name"] for p in projects}
        tasks: list[dict] = []
        for proj in projects:
            data = await self._client.get_project_data(proj["id"])
            for task in data.get("tasks", []):
                if not task.get("deleted"):
                    tasks.append(task)
        return tasks, project_map

    async def create_task(self, fields: dict) -> dict:
        return await self._client.create_task(fields)

    async def update_task(self, task_id: str, project_id: str, fields: dict) -> dict:
        return await self._client.update_task(task_id, {**fields, "projectId": project_id})

    async def delete_task(self, task_id: str, project_id: str) -> None:
        await self._client.delete_task(project_id, task_id)

    async def complete_task(self, task_id: str, project_id: str) -> None:
        await self._client.complete_task(project_id, task_id)
```

**Step 5: Run tests to confirm pass**

```bash
uv run pytest tests/test_ticktick.py -v
```

**Step 6: Commit**

```bash
git add src/tickticksync/ticktick.py tests/test_ticktick.py
git commit -m "feat: add TickTick API client wrapper"
```

---

## Task 7: Sync Engine — Change Detection

**Files:**
- Create: `src/tickticksync/sync.py`
- Create: `tests/test_sync.py`

**Step 1: Write failing tests for change detection**

```python
# tests/test_sync.py
import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from tickticksync.sync import SyncEngine, SyncChange
from tickticksync.state import StateStore, TaskMapping


@pytest.fixture
def store(tmp_db):
    s = StateStore(tmp_db)
    yield s
    s.close()


@pytest.fixture
def engine(store):
    tw = MagicMock()
    tt = AsyncMock()
    return SyncEngine(store=store, tw=tw, tt=tt)


def _mapping(**kwargs) -> TaskMapping:
    defaults = dict(
        tw_uuid="uuid-1",
        ticktick_id="tt-1",
        ticktick_project="proj-1",
        last_sync_ts=time.time() - 3600,
        tw_modified="2024-01-01T10:00:00Z",
        ticktick_modified="2024-01-01T10:00:00Z",
    )
    return TaskMapping(**{**defaults, **kwargs})


def test_no_changes_when_unmodified(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "description": "Task", "modified": "2024-01-01T10:00:00Z"}]
    tt_tasks = [{"id": "tt-1", "title": "Task", "modifiedTime": "2024-01-01T10:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert changes == []


def test_tw_only_change_detected(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "description": "Updated", "modified": "2024-06-01T10:00:00Z"}]
    tt_tasks = [{"id": "tt-1", "title": "Task", "modifiedTime": "2024-01-01T10:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert len(changes) == 1
    assert changes[0].kind == "tw_only"


def test_tt_only_change_detected(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "description": "Task", "modified": "2024-01-01T10:00:00Z"}]
    tt_tasks = [{"id": "tt-1", "title": "Updated", "modifiedTime": "2024-06-01T10:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert len(changes) == 1
    assert changes[0].kind == "tt_only"


def test_conflict_detected_when_both_changed(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "modified": "2024-06-01T00:00:00Z"}]
    tt_tasks = [{"id": "tt-1", "modifiedTime": "2024-06-02T00:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert len(changes) == 1
    assert changes[0].kind == "conflict"


def test_new_tw_task_detected(engine, store):
    tw_tasks = [{"uuid": "uuid-new", "description": "New task", "modified": "2024-06-01T00:00:00Z"}]
    tt_tasks = []
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert any(c.kind == "new_tw" for c in changes)


def test_new_tt_task_detected(engine, store):
    tw_tasks = []
    tt_tasks = [{"id": "tt-new", "title": "New", "modifiedTime": "2024-06-01T00:00:00Z", "deleted": 0}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert any(c.kind == "new_tt" for c in changes)


def test_deleted_tt_task_not_detected_as_new(engine, store):
    tw_tasks = []
    tt_tasks = [{"id": "tt-del", "title": "Gone", "modifiedTime": "x", "deleted": 1}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert not any(c.kind == "new_tt" for c in changes)
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_sync.py -v
```

**Step 3: Implement change detection in `src/tickticksync/sync.py`**

```python
from dataclasses import dataclass
from typing import Optional
from .state import StateStore, TaskMapping
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI


@dataclass
class SyncChange:
    tw_task: Optional[dict]
    tt_task: Optional[dict]
    mapping: Optional[TaskMapping]
    kind: str  # "tw_only" | "tt_only" | "conflict" | "new_tw" | "new_tt"


class SyncEngine:
    def __init__(self, state: StateStore, tw: TaskWarriorClient, tt: TickTickAPI):
        self.state = state
        self.tw = tw
        self.tt = tt

    def detect_changes(
        self, tw_tasks: list[dict], tt_tasks: list[dict]
    ) -> list[SyncChange]:
        changes: list[SyncChange] = []
        tw_by_uuid = {str(t["uuid"]): t for t in tw_tasks}
        tt_by_id = {t["id"]: t for t in tt_tasks}
        mapped_tw_uuids: set[str] = set()
        mapped_tt_ids: set[str] = set()

        for mapping in self.state.all_mappings():
            mapped_tw_uuids.add(mapping.tw_uuid)
            mapped_tt_ids.add(mapping.ticktick_id)
            tw_task = tw_by_uuid.get(mapping.tw_uuid)
            tt_task = tt_by_id.get(mapping.ticktick_id)

            tw_changed = tw_task and tw_task.get("modified") != mapping.tw_modified
            tt_changed = tt_task and tt_task.get("modifiedTime") != mapping.ticktick_modified

            if tw_changed and not tt_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, "tw_only"))
            elif tt_changed and not tw_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, "tt_only"))
            elif tw_changed and tt_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, "conflict"))

        for tw_task in tw_tasks:
            if str(tw_task["uuid"]) not in mapped_tw_uuids and not tw_task.get("ticktickid"):
                changes.append(SyncChange(tw_task, None, None, "new_tw"))

        for tt_task in tt_tasks:
            if tt_task["id"] not in mapped_tt_ids and not tt_task.get("deleted"):
                changes.append(SyncChange(None, tt_task, None, "new_tt"))

        return changes
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_sync.py -v
```

Expected: all 7 tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/sync.py tests/test_sync.py
git commit -m "feat: add sync engine change detection"
```

---

## Task 8: Sync Engine — Apply Changes

**Files:**
- Modify: `src/tickticksync/sync.py` (add `apply_changes` and helpers)
- Modify: `tests/test_sync.py` (add apply tests)

**Step 1: Add failing tests for apply_changes**

Append to `tests/test_sync.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


@pytest.mark.asyncio
async def test_apply_tw_only_pushes_to_ticktick(engine, store):
    mapping = _mapping()
    store.upsert_mapping(mapping)
    change = SyncChange(
        tw_task={"uuid": "uuid-1", "description": "Updated", "modified": "2024-06-01T00:00:00Z", "status": "pending"},
        tt_task={"id": "tt-1", "projectId": "proj-1"},
        mapping=mapping,
        kind="tw_only",
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tt.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_apply_tt_only_updates_tw(engine, store):
    mapping = _mapping()
    store.upsert_mapping(mapping)
    change = SyncChange(
        tw_task={"uuid": "uuid-1"},
        tt_task={"id": "tt-1", "title": "Updated", "status": 0, "projectId": "proj-1", "modifiedTime": "2024-06-01T00:00:00Z"},
        mapping=mapping,
        kind="tt_only",
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tw.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_apply_conflict_tw_newer_wins(engine, store):
    """TW modified later → TW wins → push to TickTick."""
    mapping = _mapping()
    store.upsert_mapping(mapping)
    change = SyncChange(
        tw_task={"uuid": "uuid-1", "description": "TW version", "modified": "2024-06-02T00:00:00Z", "status": "pending"},
        tt_task={"id": "tt-1", "title": "TT version", "modifiedTime": "2024-06-01T00:00:00Z", "projectId": "proj-1"},
        mapping=mapping,
        kind="conflict",
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tt.update_task.assert_called_once()
    engine.tw.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_apply_new_tw_creates_in_ticktick(engine, store):
    engine.tt.create_task.return_value = {"id": "tt-new", "projectId": "proj-1", "modifiedTime": "x"}
    change = SyncChange(
        tw_task={"uuid": "uuid-new", "description": "New", "modified": "2024-06-01T00:00:00Z", "status": "pending"},
        tt_task=None,
        mapping=None,
        kind="new_tw",
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tt.create_task.assert_called_once()
    assert store.get_by_tw_uuid("uuid-new") is not None


@pytest.mark.asyncio
async def test_apply_new_tt_creates_in_tw(engine, store):
    engine.tw.create_task.return_value = "uuid-created"
    change = SyncChange(
        tw_task=None,
        tt_task={"id": "tt-new", "title": "New", "status": 0, "projectId": "proj-1", "modifiedTime": "2024-06-01T00:00:00Z"},
        mapping=None,
        kind="new_tt",
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tw.create_task.assert_called_once()
    assert store.get_by_ticktick_id("tt-new") is not None
```

**Step 2: Run to confirm new tests fail**

```bash
uv run pytest tests/test_sync.py -v -k "apply"
```

**Step 3: Add `apply_changes` to `src/tickticksync/sync.py`**

```python
import time
from .mapper import tw_task_to_ticktick, ticktick_task_to_tw

# Add inside SyncEngine class:

    async def apply_changes(
        self, changes: list[SyncChange], project_map: dict[str, str]
    ) -> None:
        for change in changes:
            match change.kind:
                case "tw_only":
                    await self._push_tw_to_tt(change, project_map)
                case "tt_only":
                    await self._push_tt_to_tw(change, project_map)
                case "conflict":
                    await self._resolve_conflict(change, project_map)
                case "new_tw":
                    await self._create_in_tt(change, project_map)
                case "new_tt":
                    await self._create_in_tw(change, project_map)

    async def _push_tw_to_tt(self, change: SyncChange, project_map: dict) -> None:
        tt_fields = tw_task_to_ticktick(change.tw_task, change.mapping.ticktick_project)
        await self.tt.update_task(
            change.mapping.ticktick_id, change.mapping.ticktick_project, tt_fields
        )
        self._update_mapping_timestamps(change)

    async def _push_tt_to_tw(self, change: SyncChange, project_map: dict) -> None:
        project_name = project_map.get(change.tt_task.get("projectId", ""), "")
        tw_fields = ticktick_task_to_tw(change.tt_task, project_name)
        self.tw.update_task(str(change.tw_task["uuid"]), tw_fields)
        self._update_mapping_timestamps(change)

    async def _resolve_conflict(self, change: SyncChange, project_map: dict) -> None:
        tw_mod = change.tw_task.get("modified", "")
        tt_mod = change.tt_task.get("modifiedTime", "")
        if tw_mod >= tt_mod:
            await self._push_tw_to_tt(change, project_map)
        else:
            await self._push_tt_to_tw(change, project_map)

    async def _create_in_tt(self, change: SyncChange, project_map: dict) -> None:
        # Find default project id from project_map (reverse lookup)
        default_name = "inbox"
        project_id = next(
            (pid for pid, name in project_map.items() if name.lower() == default_name),
            next(iter(project_map), ""),
        )
        tt_fields = tw_task_to_ticktick(change.tw_task, project_id)
        created = await self.tt.create_task(tt_fields)
        self.state.upsert_mapping(TaskMapping(
            tw_uuid=str(change.tw_task["uuid"]),
            ticktick_id=created["id"],
            ticktick_project=created.get("projectId", project_id),
            last_sync_ts=time.time(),
            tw_modified=change.tw_task.get("modified"),
            ticktick_modified=created.get("modifiedTime"),
        ))

    async def _create_in_tw(self, change: SyncChange, project_map: dict) -> None:
        project_name = project_map.get(change.tt_task.get("projectId", ""), "")
        tw_fields = ticktick_task_to_tw(change.tt_task, project_name)
        new_uuid = self.tw.create_task(tw_fields)
        self.state.upsert_mapping(TaskMapping(
            tw_uuid=new_uuid,
            ticktick_id=change.tt_task["id"],
            ticktick_project=change.tt_task.get("projectId", ""),
            last_sync_ts=time.time(),
            tw_modified=None,
            ticktick_modified=change.tt_task.get("modifiedTime"),
        ))

    def _update_mapping_timestamps(self, change: SyncChange) -> None:
        if not change.mapping:
            return
        change.mapping.last_sync_ts = time.time()
        change.mapping.tw_modified = change.tw_task.get("modified") if change.tw_task else None
        change.mapping.ticktick_modified = change.tt_task.get("modifiedTime") if change.tt_task else None
        self.state.upsert_mapping(change.mapping)
```

**Step 4: Run all sync tests**

```bash
uv run pytest tests/test_sync.py -v
```

Expected: all tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/sync.py tests/test_sync.py
git commit -m "feat: add sync engine apply changes with conflict resolution"
```

---

## Task 9: Hook Scripts

**Files:**
- Create: `src/tickticksync/hooks.py`
- Create: `tests/test_hooks.py`

**Step 1: Write failing tests**

```python
# tests/test_hooks.py
import json
import socket
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
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
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_hooks.py -v
```

**Step 3: Implement `src/tickticksync/hooks.py`**

```python
import json
import socket
import sys
from pathlib import Path
from typing import Callable, Optional


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
    _original = sys.stdin.readline()   # first line: original task (discard)
    modified = json.loads(sys.stdin.readline())
    cfg = load_config()
    send_to_daemon(modified, cfg.sync.socket_path, cfg.sync.queue_path)
    print(json.dumps(modified))


def _append_to_queue(task: dict, queue_path: Path) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = json.loads(queue_path.read_text()) if queue_path.exists() else []
    existing.append(task)
    queue_path.write_text(json.dumps(existing))
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_hooks.py -v
```

Expected: all 5 tests PASS

**Step 5: Commit**

```bash
git add src/tickticksync/hooks.py tests/test_hooks.py
git commit -m "feat: add TW hook handler with queue fallback"
```

---

## Task 10: Daemon

**Files:**
- Create: `src/tickticksync/daemon.py`
- Create: `tests/test_daemon.py`

**Step 1: Write failing tests**

```python
# tests/test_daemon.py
import asyncio
import json
import socket
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
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
async def test_daemon_processes_queue_item(tmp_db, tmp_path):
    sync_engine = AsyncMock()
    sync_engine.detect_changes.return_value = []
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({"uuid": "u1", "description": "Task"})

    daemon = Daemon(
        sync_engine=sync_engine,
        queue=queue,
        socket_path=str(tmp_path / "test.sock"),
        queue_path=str(tmp_path / "queue.json"),
        poll_interval=9999,  # prevent timer from firing
    )
    await daemon._flush_hook_queue()
    # Should have pushed task to TickTick (processed by sync engine)
    assert queue.empty()
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_daemon.py -v
```

**Step 3: Implement `src/tickticksync/daemon.py`**

```python
import asyncio
import json
import os
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
        # Hook delivers a single TW task — run a targeted sync for it
        tw_tasks = [task]
        tt_tasks, project_map = await self._engine.tt.get_all_tasks()
        changes = self._engine.detect_changes(tw_tasks, tt_tasks)
        await self._engine.apply_changes(changes, project_map)

    async def _run_sync_cycle(self) -> None:
        tw_tasks = self._engine.tw.get_pending_tasks()
        tt_tasks, project_map = await self._engine.tt.get_all_tasks()
        changes = self._engine.detect_changes(tw_tasks, tt_tasks)
        await self._engine.apply_changes(changes, project_map)
        self._engine.state.set_state("last_poll_ts", str(time.time()))
```

**Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_daemon.py -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add src/tickticksync/daemon.py tests/test_daemon.py
git commit -m "feat: add asyncio daemon with socket listener and poll loop"
```

---

## Task 11: CLI

**Files:**
- Create: `src/tickticksync/cli.py`
- Create: `tests/test_cli.py`

**Step 1: Write failing tests**

```python
# tests/test_cli.py
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
from click.testing import CliRunner
from tickticksync.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_status_no_config(runner, tmp_path):
    with patch("tickticksync.cli.load_config", side_effect=FileNotFoundError):
        result = runner.invoke(cli, ["status"])
    assert result.exit_code != 0 or "not found" in result.output.lower()


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
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_cli.py -v
```

**Step 3: Implement `src/tickticksync/cli.py`**

```python
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import click

from .config import load_config, Config
from .state import StateStore
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI
from .sync import SyncEngine
from .daemon import Daemon

PID_FILE = Path("~/.local/share/tickticksync/tickticksync.pid").expanduser()
HOOKS_DIR = Path("~/.local/share/task/hooks").expanduser()


@click.group()
def cli() -> None:
    """TickTick ↔ TaskWarrior bidirectional sync."""


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

    cfg = load_config(config_path)

    # Register TW UDA
    tw = TaskWarriorClient()
    tw.register_uda("ticktickid", "string", "TickTick ID")
    click.echo("Registered TW UDA: ticktickid")

    # Install hook scripts
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for hook_name, entry in [
        ("on-add.tickticksync", "tickticksync.hooks:on_add_hook"),
        ("on-modify.tickticksync", "tickticksync.hooks:on_modify_hook"),
    ]:
        hook_path = HOOKS_DIR / hook_name
        hook_path.write_text(
            f"#!/usr/bin/env python3\nfrom {entry.split(':')[0]} import {entry.split(':')[1]}\n{entry.split(':')[1]}()\n"
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
    tt = TickTickAPI(cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path))
    engine = SyncEngine(state=state, tw=tw, tt=tt)

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

    # Child process
    os.setsid()
    state = StateStore(cfg.db_path)
    tw = TaskWarriorClient()
    tt = TickTickAPI(cfg.ticktick.client_id, cfg.ticktick.client_secret, str(cfg.token_path))
    engine = SyncEngine(state=state, tw=tw, tt=tt)
    import asyncio as _asyncio
    queue: _asyncio.Queue = _asyncio.Queue()
    d = Daemon(
        sync_engine=engine,
        queue=queue,
        socket_path=cfg.sync.socket_path,
        queue_path=cfg.sync.queue_path,
        poll_interval=cfg.sync.poll_interval,
    )
    _asyncio.run(d.run())


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
        os.kill(pid, 0)  # signal 0 = check existence
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
        if last_poll else "never"
    )
    click.echo(f"Mapped tasks : {count}")
    click.echo(f"Last sync    : {last_poll_str}")
    state.close()
```

**Step 4: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: all tests PASS

**Step 5: Smoke test the CLI**

```bash
uv run tickticksync --help
uv run tickticksync daemon status
```

Expected: help text shown, "not running" status

**Step 6: Commit**

```bash
git add src/tickticksync/cli.py tests/test_cli.py
git commit -m "feat: add CLI with init, sync, daemon, status commands"
```

---

## Final Verification

```bash
uv run pytest tests/ -v --tb=short
```

All tests must pass before considering the implementation complete.

---

## Known Limitations (document as GitHub issues after shipping)

1. `daemon start` uses `os.fork()` — Unix only (not Windows)
2. TickTick-side deletions are detected via `deleted=1` field, not a deletion event
3. No retry logic on TickTick API failures
4. OAuth token refresh not handled — expires after ~6 months, requires `tickticksync init` re-run
