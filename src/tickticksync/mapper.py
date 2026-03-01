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
        if not (a["description"].startswith("[x] ") or a["description"].startswith("[ ] "))
    ]
    if content_parts:
        tt["content"] = "\n".join(content_parts)
    return tt


def ticktick_task_to_tw(tt_task: dict, project_name: str) -> dict:
    """Convert a TickTick task dict to a TaskWarrior task dict."""
    tw_priority = TT_TO_TW_PRIORITY.get(tt_task.get("priority", 0))
    tw: dict = {
        "description": tt_task["title"],
        "project": project_name,
        "status": "completed" if tt_task.get("status") == 2 else "pending",
    }
    if tw_priority is not None:
        tw["priority"] = tw_priority
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
