import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from tickticksync.sync import SyncEngine, SyncChange, ChangeKind
from tickticksync.state import StateStore, TaskMapping
from tickticksync.config import ProjectMapping


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


@pytest.mark.asyncio
async def test_apply_tw_only_pushes_to_ticktick(engine, store):
    mapping = _mapping()
    store.upsert_mapping(mapping)
    change = SyncChange(
        tw_task={"uuid": "uuid-1", "description": "Updated", "modified": "2024-06-01T00:00:00Z", "status": "pending"},
        tt_task={"id": "tt-1", "projectId": "proj-1"},
        mapping=mapping,
        kind=ChangeKind.TW_ONLY,
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
        kind=ChangeKind.TT_ONLY,
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tw.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_apply_conflict_tw_newer_wins(engine, store):
    """TW modified later -> TW wins -> push to TickTick."""
    mapping = _mapping()
    store.upsert_mapping(mapping)
    change = SyncChange(
        tw_task={"uuid": "uuid-1", "description": "TW version", "modified": "2024-06-02T00:00:00Z", "status": "pending"},
        tt_task={"id": "tt-1", "title": "TT version", "modifiedTime": "2024-06-01T00:00:00Z", "projectId": "proj-1"},
        mapping=mapping,
        kind=ChangeKind.CONFLICT,
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
        kind=ChangeKind.NEW_TW,
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
        kind=ChangeKind.NEW_TT,
    )
    project_map = {"proj-1": "Work"}
    await engine.apply_changes([change], project_map)
    engine.tw.create_task.assert_called_once()
    assert store.get_by_ticktick_id("tt-new") is not None


def test_engine_builds_lookup_dicts():
    """SyncEngine builds tt_to_tw and tw_to_tt dicts from project mappings."""
    store = MagicMock()
    tw = MagicMock()
    tt = AsyncMock()
    mappings = [
        ProjectMapping(ticktick="Inbox", taskwarrior="inbox"),
        ProjectMapping(ticktick="Work", taskwarrior="work"),
    ]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)
    assert engine._tt_to_tw == {"Inbox": "inbox", "Work": "work"}
    assert engine._tw_to_tt == {"inbox": "Inbox", "work": "Work"}


def test_engine_accepts_empty_mappings():
    """SyncEngine works with empty mappings list."""
    store = MagicMock()
    tw = MagicMock()
    tt = AsyncMock()
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=[])
    assert engine._tt_to_tw == {}
    assert engine._tw_to_tt == {}
