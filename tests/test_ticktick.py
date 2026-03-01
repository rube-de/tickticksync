"""Tests for TickTickAPI wrapper.

SDK installed: ticktick-sdk==0.4.3
Import path: from ticktick_sdk import TickTickClient
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
