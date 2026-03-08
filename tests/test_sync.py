import logging
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
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    return SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)


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
    tw_tasks = [{"uuid": "uuid-1", "description": "Task", "modified": "2024-01-01T10:00:00Z", "project": "work"}]
    tt_tasks = [{"id": "tt-1", "title": "Task", "modifiedTime": "2024-01-01T10:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert changes == []


def test_tw_only_change_detected(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "description": "Updated", "modified": "2024-06-01T10:00:00Z", "project": "work"}]
    tt_tasks = [{"id": "tt-1", "title": "Task", "modifiedTime": "2024-01-01T10:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert len(changes) == 1
    assert changes[0].kind == "tw_only"


def test_tt_only_change_detected(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "description": "Task", "modified": "2024-01-01T10:00:00Z", "project": "work"}]
    tt_tasks = [{"id": "tt-1", "title": "Updated", "modifiedTime": "2024-06-01T10:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert len(changes) == 1
    assert changes[0].kind == "tt_only"


def test_conflict_detected_when_both_changed(engine, store):
    store.upsert_mapping(_mapping())
    tw_tasks = [{"uuid": "uuid-1", "modified": "2024-06-01T00:00:00Z", "project": "work"}]
    tt_tasks = [{"id": "tt-1", "modifiedTime": "2024-06-02T00:00:00Z"}]
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert len(changes) == 1
    assert changes[0].kind == "conflict"


def test_new_tw_task_detected(engine):
    tw_tasks = [{"uuid": "uuid-new", "description": "New task", "modified": "2024-06-01T00:00:00Z", "project": "work"}]
    tt_tasks = []
    changes = engine.detect_changes(tw_tasks, tt_tasks)
    assert any(c.kind == "new_tw" for c in changes)


def test_new_tt_task_detected(engine):
    tw_tasks = []
    tt_tasks = [{"id": "tt-new", "title": "New", "projectId": "p1", "modifiedTime": "2024-06-01T00:00:00Z", "deleted": 0}]
    changes = engine.detect_changes(tw_tasks, tt_tasks, mapped_tt_project_ids={"p1"})
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
        tw_task={"uuid": "uuid-new", "description": "New", "modified": "2024-06-01T00:00:00Z", "status": "pending", "project": "work"},
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


@pytest.mark.asyncio
async def test_pull_filters_unmapped_ticktick_projects(store):
    """Tasks from unmapped TickTick projects are ignored during pull."""
    tw = MagicMock()
    tw.get_pending_tasks.return_value = []
    tw.create_task.return_value = "uuid-created"
    tt = AsyncMock()
    tt.get_all_tasks.return_value = (
        [
            {"id": "tt-1", "title": "Mapped", "projectId": "pid-1", "modifiedTime": "x"},
            {"id": "tt-2", "title": "Unmapped", "projectId": "pid-2", "modifiedTime": "x"},
        ],
        {"pid-1": "Work", "pid-2": "Personal"},
    )
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    changes = await engine.run_cycle()
    # Only the mapped project's task should produce a NEW_TT change
    new_tt = [c for c in changes if c.kind == "new_tt"]
    assert len(new_tt) == 1
    assert new_tt[0].tt_task["id"] == "tt-1"


@pytest.mark.asyncio
async def test_empty_mappings_logs_warning_and_skips(store, caplog):
    """Empty mappings list results in warning and no sync."""
    tw = MagicMock()
    tw.get_pending_tasks.return_value = [{"uuid": "u1", "description": "X", "modified": "x"}]
    tt = AsyncMock()
    tt.get_all_tasks.return_value = (
        [{"id": "tt-1", "title": "T", "projectId": "p1", "modifiedTime": "x"}],
        {"p1": "Inbox"},
    )
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=[])

    with caplog.at_level(logging.WARNING):
        changes = await engine.run_cycle()
    assert changes == []
    assert "no project mappings" in caplog.text.lower()


@pytest.mark.asyncio
async def test_create_in_tt_uses_mapping(store):
    """NEW_TW tasks are created in the TickTick project from the mapping."""
    tw = MagicMock()
    tt = AsyncMock()
    tt.create_task.return_value = {"id": "tt-new", "projectId": "pid-work", "modifiedTime": "x"}
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    change = SyncChange(
        tw_task={"uuid": "u1", "description": "New task", "modified": "x", "status": "pending", "project": "work"},
        tt_task=None,
        mapping=None,
        kind=ChangeKind.NEW_TW,
    )
    project_map = {"pid-work": "Work", "pid-personal": "Personal"}
    await engine.apply_changes([change], project_map)

    tt.create_task.assert_called_once()
    call_args = tt.create_task.call_args[0][0]
    assert call_args["projectId"] == "pid-work"


@pytest.mark.asyncio
async def test_create_in_tt_skips_unmapped_tw_project(store):
    """NEW_TW tasks whose project doesn't match any mapping are skipped."""
    tw = MagicMock()
    tt = AsyncMock()
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    change = SyncChange(
        tw_task={"uuid": "u1", "description": "Unmapped", "modified": "x", "status": "pending", "project": "personal"},
        tt_task=None,
        mapping=None,
        kind=ChangeKind.NEW_TW,
    )
    project_map = {"pid-work": "Work"}
    await engine.apply_changes([change], project_map)

    tt.create_task.assert_not_called()
    assert store.get_by_tw_uuid("u1") is None


@pytest.mark.asyncio
async def test_create_in_tw_uses_mapped_project_name(store):
    """NEW_TT tasks use the mapped TaskWarrior project name, not the TickTick name."""
    tw = MagicMock()
    tw.create_task.return_value = "uuid-created"
    tt = AsyncMock()
    mappings = [ProjectMapping(ticktick="My Work List", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    change = SyncChange(
        tw_task=None,
        tt_task={"id": "tt-1", "title": "From TT", "status": 0, "projectId": "pid-1", "modifiedTime": "x"},
        mapping=None,
        kind=ChangeKind.NEW_TT,
    )
    project_map = {"pid-1": "My Work List"}
    await engine.apply_changes([change], project_map)

    tw.create_task.assert_called_once()
    call_args = tw.create_task.call_args[0][0]
    assert call_args["project"] == "work"


def test_detect_changes_skips_new_tw_with_unmapped_project(store):
    """NEW_TW is not emitted for TW tasks whose project has no mapping."""
    tw = MagicMock()
    tt = AsyncMock()
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    tw_tasks = [
        {"uuid": "u1", "description": "Mapped", "modified": "x", "project": "work"},
        {"uuid": "u2", "description": "Unmapped", "modified": "x", "project": "personal"},
        {"uuid": "u3", "description": "No project", "modified": "x"},
    ]
    changes = engine.detect_changes(tw_tasks, [])

    new_tw = [c for c in changes if c.kind == "new_tw"]
    assert len(new_tw) == 1
    assert new_tw[0].tw_task["uuid"] == "u1"


@pytest.mark.asyncio
async def test_run_cycle_only_syncs_mapped_projects(store):
    """Full cycle: only mapped projects produce sync changes."""
    tw = MagicMock()
    tw.get_pending_tasks.return_value = [
        {"uuid": "u1", "description": "Work task", "modified": "x", "status": "pending", "project": "work"},
        {"uuid": "u2", "description": "Personal task", "modified": "x", "status": "pending", "project": "personal"},
    ]
    tt = AsyncMock()
    tt.get_all_tasks.return_value = (
        [
            {"id": "tt-1", "title": "From Work", "projectId": "pid-1", "modifiedTime": "x", "status": 0},
            {"id": "tt-2", "title": "From Hobby", "projectId": "pid-3", "modifiedTime": "x", "status": 0},
        ],
        {"pid-1": "Work", "pid-2": "Personal", "pid-3": "Hobby"},
    )
    tt.create_task.return_value = {"id": "tt-new", "projectId": "pid-1", "modifiedTime": "x"}
    tw.create_task.return_value = "uuid-created"
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    changes = await engine.run_cycle()

    # Only u1 (work) detected as NEW_TW; u2 (personal) filtered in detect_changes
    new_tw = [c for c in changes if c.kind == "new_tw"]
    assert len(new_tw) == 1
    assert new_tw[0].tw_task["uuid"] == "u1"

    # Only tt-1 (Work) detected as NEW_TT; tt-2 (Hobby) filtered in run_cycle
    new_tt = [c for c in changes if c.kind == "new_tt"]
    assert len(new_tt) == 1
    assert new_tt[0].tt_task["id"] == "tt-1"

    # Only u1's mapping was persisted
    assert store.get_by_tw_uuid("u1") is not None
    assert store.get_by_tw_uuid("u2") is None


@pytest.mark.asyncio
async def test_mapped_tt_task_moved_to_unmapped_project_skipped(store):
    """A mapped TT task that moves to an unmapped project should be skipped
    at detection (symmetric with the TW-side guard), preventing repeated
    stale detection and false CONFLICTs."""
    tw = MagicMock()
    tw.get_pending_tasks.return_value = [
        {"uuid": "uuid-1", "description": "Task", "modified": "2024-01-01T10:00:00Z", "project": "work"},
    ]
    tt = AsyncMock()
    # Task moved from "Work" (pid-1) to "Personal" (pid-2, unmapped)
    tt.get_all_tasks.return_value = (
        [{"id": "tt-1", "title": "Moved", "projectId": "pid-2", "modifiedTime": "2024-06-01T00:00:00Z"}],
        {"pid-1": "Work", "pid-2": "Personal"},
    )
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)
    store.upsert_mapping(_mapping(ticktick_project="pid-1"))

    changes = await engine.run_cycle()
    # TT task moved to unmapped project — guard skips change detection entirely
    tt_only = [c for c in changes if c.kind == "tt_only"]
    assert len(tt_only) == 0


@pytest.mark.asyncio
async def test_new_tt_from_unmapped_project_filtered_by_project_ids(store):
    """NEW_TT tasks from unmapped projects are skipped when mapped_tt_project_ids is provided."""
    tw = MagicMock()
    tt = AsyncMock()
    mappings = [ProjectMapping(ticktick="Work", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    tt_tasks = [
        {"id": "tt-1", "title": "Mapped", "projectId": "pid-1", "modifiedTime": "x"},
        {"id": "tt-2", "title": "Unmapped", "projectId": "pid-2", "modifiedTime": "x"},
    ]
    changes = engine.detect_changes([], tt_tasks, mapped_tt_project_ids={"pid-1"})
    new_tt = [c for c in changes if c.kind == "new_tt"]
    assert len(new_tt) == 1
    assert new_tt[0].tt_task["id"] == "tt-1"


@pytest.mark.asyncio
async def test_run_cycle_mapped_pull_uses_tw_project_name(store):
    """Pull path creates TW tasks with the mapped TaskWarrior project name."""
    tw = MagicMock()
    tw.get_pending_tasks.return_value = []
    tw.create_task.return_value = "uuid-created"
    tt = AsyncMock()
    tt.get_all_tasks.return_value = (
        [{"id": "tt-1", "title": "From TT", "projectId": "pid-1", "modifiedTime": "x", "status": 0}],
        {"pid-1": "My Work List"},
    )
    mappings = [ProjectMapping(ticktick="My Work List", taskwarrior="work")]
    engine = SyncEngine(store=store, tw=tw, tt=tt, project_mappings=mappings)

    await engine.run_cycle()

    tw.create_task.assert_called_once()
    call_args = tw.create_task.call_args[0][0]
    assert call_args["project"] == "work"
