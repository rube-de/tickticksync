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
