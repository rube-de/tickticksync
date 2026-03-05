"""Tests for TickTickAPI wrapper.

SDK installed: ticktick-sdk==0.4.3
Import path: from ticktick_sdk import TickTickClient
"""

import pytest
from unittest.mock import AsyncMock, patch

from tickticksync.ticktick import TickTickAPI


@pytest.fixture
def mock_sdk():
    # Patch at the import site inside ticktick.py.
    # TickTickAPI instantiates TickTickClient and stores it as self._client,
    # then delegates to self._client.get_projects(), self._client.get_project_data(),
    # self._client.create_task(), etc.
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


@pytest.mark.asyncio
async def test_create_task_does_not_mutate_input(mock_sdk):
    mock_sdk.create_task.return_value = {"id": "new-1", "title": "Test"}
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    original = {"title": "Test", "projectId": "p1", "dueDate": "2024-06-01"}
    original_copy = dict(original)
    await api.create_task(original)
    assert original == original_copy  # must not be mutated


@pytest.mark.asyncio
async def test_update_task(mock_sdk):
    mock_sdk.update_task.return_value = {"id": "t1", "title": "Updated"}
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    result = await api.update_task("t1", "proj-1", {"title": "Updated"})
    assert result["id"] == "t1"
    mock_sdk.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_update_task_strips_id_fields(mock_sdk):
    """Fields dict containing 'id' or 'projectId' must not cause TypeError."""
    mock_sdk.update_task.return_value = {"id": "t1", "title": "Updated"}
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    # passing id and projectId inside fields should not raise
    await api.update_task("t1", "proj-1", {"id": "t1", "projectId": "proj-1", "title": "Updated"})
    mock_sdk.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_delete_task(mock_sdk):
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    await api.delete_task("tt-1", "proj-1")
    mock_sdk.delete_task.assert_called_once()


@pytest.mark.asyncio
async def test_complete_task(mock_sdk):
    api = TickTickAPI("id", "secret", "/tmp/token.json")
    await api.complete_task("tt-1", "proj-1")
    mock_sdk.complete_task.assert_called_once()


# ---------------------------------------------------------------------------
# V2 (password auth) path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_all_tasks_v2_path(mock_sdk):
    """When username is set, get_all_tasks() uses V2 single-call path."""
    mock_sdk.get_all_tasks_v2.return_value = [
        {"id": "t1", "title": "V2 Task", "deleted": 0},
        {"id": "t2", "title": "Deleted V2", "deleted": 1},
    ]
    mock_sdk.get_projects.return_value = [{"id": "p1", "name": "Work"}]

    api = TickTickAPI("id", "secret", "/tmp/token.json", username="user@example.com", password="pw", use_v2_tasks=True)
    tasks, project_map = await api.get_all_tasks()

    mock_sdk.get_all_tasks_v2.assert_called_once()
    # per-project fetch must NOT be called on V2 path
    mock_sdk.get_project_data.assert_not_called()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t1"
    assert project_map["p1"] == "Work"


@pytest.mark.asyncio
async def test_get_all_tasks_v1_path_unchanged(mock_sdk):
    """Without username, get_all_tasks() still uses V1 per-project path."""
    mock_sdk.get_projects.return_value = [{"id": "p1", "name": "Work"}]
    mock_sdk.get_project_data.return_value = {"tasks": [{"id": "t1", "deleted": 0}]}

    api = TickTickAPI("id", "secret", "/tmp/token.json")
    tasks, _ = await api.get_all_tasks()

    mock_sdk.get_all_tasks_v2.assert_not_called()
    mock_sdk.get_project_data.assert_called_once_with("p1")
    assert len(tasks) == 1
