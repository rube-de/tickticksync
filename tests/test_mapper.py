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


def test_tw_to_tt_bracket_annotation_preserved_in_content():
    """Annotations starting with [ but not subtask markers must survive into content."""
    tw = {
        "description": "Task",
        "status": "pending",
        "annotations": [{"description": "[URGENT] Call doctor"}],
    }
    result = tw_task_to_ticktick(tw, project_id="p")
    assert "[URGENT] Call doctor" in result["content"]


def test_tt_to_tw_unmapped_priority_omits_key():
    """TickTick priority values 2 and 4 are not in the map — TW dict must not contain None."""
    tt = {"id": "tt-1", "title": "Task", "status": 0, "priority": 2}
    result = ticktick_task_to_tw(tt, project_name="P")
    assert "priority" not in result or result.get("priority") is not None
