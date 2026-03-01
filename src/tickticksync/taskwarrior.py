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

    def _get_task(self, uuid: str) -> tasklib.Task:
        tasks = self._tw.tasks.filter(uuid=uuid)
        if not tasks:
            raise ValueError(f"Task {uuid} not found")
        return tasks[0]

    def update_task(self, uuid: str, fields: dict) -> None:
        task = self._get_task(uuid)
        for k, v in fields.items():
            task[k] = v
        task.save()

    def complete_task(self, uuid: str) -> None:
        self._get_task(uuid).done()

    def delete_task(self, uuid: str) -> None:
        self._get_task(uuid).delete()

    def register_uda(self, name: str, type_: str, label: str) -> None:
        subprocess.run(
            ["task", "rc.confirmation:off", "config", f"uda.{name}.type", type_],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["task", "rc.confirmation:off", "config", f"uda.{name}.label", label],
            check=True, capture_output=True,
        )

    @staticmethod
    def _task_to_dict(task: tasklib.Task) -> dict:
        return dict(task)
