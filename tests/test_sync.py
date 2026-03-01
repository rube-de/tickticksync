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
