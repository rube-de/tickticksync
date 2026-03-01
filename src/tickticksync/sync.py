from dataclasses import dataclass
from typing import Optional

from .state import StateStore, TaskMapping
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI


@dataclass
class SyncChange:
    tw_task: Optional[dict]
    tt_task: Optional[dict]
    mapping: Optional[TaskMapping]
    kind: str  # "tw_only" | "tt_only" | "conflict" | "new_tw" | "new_tt"


class SyncEngine:
    def __init__(self, store: StateStore, tw: TaskWarriorClient, tt: TickTickAPI):
        self.store = store
        self.tw = tw
        self.tt = tt

    def detect_changes(
        self, tw_tasks: list[dict], tt_tasks: list[dict]
    ) -> list[SyncChange]:
        changes: list[SyncChange] = []
        tw_by_uuid = {str(t["uuid"]): t for t in tw_tasks}
        tt_by_id = {t["id"]: t for t in tt_tasks}
        mapped_tw_uuids: set[str] = set()
        mapped_tt_ids: set[str] = set()

        for mapping in self.store.all_mappings():
            mapped_tw_uuids.add(mapping.tw_uuid)
            mapped_tt_ids.add(mapping.ticktick_id)
            tw_task = tw_by_uuid.get(mapping.tw_uuid)
            tt_task = tt_by_id.get(mapping.ticktick_id)

            tw_changed = tw_task and tw_task.get("modified") != mapping.tw_modified
            tt_changed = tt_task and tt_task.get("modifiedTime") != mapping.ticktick_modified

            if tw_changed and not tt_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, "tw_only"))
            elif tt_changed and not tw_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, "tt_only"))
            elif tw_changed and tt_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, "conflict"))

        for tw_task in tw_tasks:
            if str(tw_task["uuid"]) not in mapped_tw_uuids and not tw_task.get("ticktickid"):
                changes.append(SyncChange(tw_task, None, None, "new_tw"))

        for tt_task in tt_tasks:
            if tt_task["id"] not in mapped_tt_ids and not tt_task.get("deleted"):
                changes.append(SyncChange(None, tt_task, None, "new_tt"))

        return changes
