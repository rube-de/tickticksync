import pytest
from unittest.mock import MagicMock, patch, call
from tickticksync.taskwarrior import TaskWarriorClient


@pytest.fixture
def mock_tw():
    with patch("tickticksync.taskwarrior.tasklib") as mock_lib:
        mock_warrior = MagicMock()
        mock_lib.TaskWarrior.return_value = mock_warrior
        mock_lib.Task = MagicMock()
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
