import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from .config import ProjectMapping
from .mapper import ticktick_task_to_tw, tw_task_to_ticktick
from .state import StateStore, TaskMapping
from .taskwarrior import TaskWarriorClient
from .ticktick import TickTickAPI

logger = logging.getLogger(__name__)


class ChangeKind(StrEnum):
    TW_ONLY = "tw_only"
    TT_ONLY = "tt_only"
    CONFLICT = "conflict"
    NEW_TW = "new_tw"
    NEW_TT = "new_tt"


@dataclass
class SyncChange:
    tw_task: Optional[dict]
    tt_task: Optional[dict]
    mapping: Optional[TaskMapping]
    kind: ChangeKind


class SyncEngine:
    def __init__(
        self,
        store: StateStore,
        tw: TaskWarriorClient,
        tt: TickTickAPI,
        *,
        project_mappings: list[ProjectMapping] | None = None,
    ):
        self.store = store
        self.tw = tw
        self.tt = tt
        mappings = project_mappings or []
        self._tt_to_tw: dict[str, str] = {m.ticktick: m.taskwarrior for m in mappings}
        self._tw_to_tt: dict[str, str] = {m.taskwarrior: m.ticktick for m in mappings}
        if len(self._tt_to_tw) != len(mappings) or len(self._tw_to_tt) != len(mappings):
            raise ValueError(
                "Project mappings must be one-to-one; duplicate TickTick or TaskWarrior names found."
            )

    def detect_changes(
        self,
        tw_tasks: list[dict],
        tt_tasks: list[dict],
        *,
        mapped_tt_project_ids: set[str] | None = None,
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

            # Skip mapped TW tasks that moved to an unmapped project
            if tw_task is not None and self._tw_to_tt:
                tw_project = tw_task.get("project", "")
                if tw_project not in self._tw_to_tt:
                    logger.debug(
                        "Skipping change detection for mapping %s — TW task moved to unmapped project %r",
                        mapping.tw_uuid, tw_project,
                    )
                    continue

            # Skip mapped TT tasks that moved to an unmapped project
            if tt_task is not None and mapped_tt_project_ids is not None:
                tt_project_id = tt_task.get("projectId", "")
                if tt_project_id not in mapped_tt_project_ids:
                    logger.debug(
                        "Skipping change detection for mapping %s — TT task moved to unmapped project %r",
                        mapping.ticktick_id, tt_project_id,
                    )
                    continue

            tw_changed = tw_task and tw_task.get("modified") != mapping.tw_modified
            tt_changed = tt_task and tt_task.get("modifiedTime") != mapping.ticktick_modified

            if tw_changed and not tt_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, ChangeKind.TW_ONLY))
            elif tt_changed and not tw_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, ChangeKind.TT_ONLY))
            elif tw_changed and tt_changed:
                changes.append(SyncChange(tw_task, tt_task, mapping, ChangeKind.CONFLICT))

        for tw_task in tw_tasks:
            if str(tw_task["uuid"]) not in mapped_tw_uuids and not tw_task.get("ticktickid"):
                tw_project = tw_task.get("project", "")
                if not self._tt_to_tw or tw_project not in self._tw_to_tt:
                    continue
                changes.append(SyncChange(tw_task, None, None, ChangeKind.NEW_TW))

        for tt_task in tt_tasks:
            if tt_task["id"] not in mapped_tt_ids and not tt_task.get("deleted"):
                # Only import new TT tasks from mapped projects
                if mapped_tt_project_ids is not None:
                    if tt_task.get("projectId") not in mapped_tt_project_ids:
                        continue
                elif self._tt_to_tw:
                    logger.debug(
                        "Skipping NEW_TT for task %s — "
                        "mapped_tt_project_ids not provided",
                        tt_task["id"],
                    )
                    continue
                changes.append(SyncChange(None, tt_task, None, ChangeKind.NEW_TT))

        return changes

    async def run_cycle(self, tw_tasks: list[dict] | None = None) -> list[SyncChange]:
        """Run a full sync cycle: fetch, detect, apply. Returns detected changes."""
        if not self._tt_to_tw:
            logger.warning("No project mappings configured — skipping sync cycle")
            return []

        if tw_tasks is None:
            tw_tasks = self.tw.get_pending_tasks()
        tt_tasks, project_map = await self.tt.get_all_tasks()

        # Build set of mapped TickTick project IDs (used to gate NEW_TT only)
        mapped_tt_project_ids = {
            pid for pid, name in project_map.items() if name in self._tt_to_tw
        }

        changes = self.detect_changes(
            tw_tasks, tt_tasks, mapped_tt_project_ids=mapped_tt_project_ids
        )
        with self.store.batch():
            await self.apply_changes(changes, project_map)
        return changes

    async def apply_changes(
        self, changes: list[SyncChange], project_map: dict[str, str]
    ) -> None:
        for change in changes:
            match change.kind:
                case ChangeKind.TW_ONLY:
                    await self._push_tw_to_tt(change, project_map)
                case ChangeKind.TT_ONLY:
                    await self._push_tt_to_tw(change, project_map)
                case ChangeKind.CONFLICT:
                    await self._resolve_conflict(change, project_map)
                case ChangeKind.NEW_TW:
                    await self._create_in_tt(change, project_map)
                case ChangeKind.NEW_TT:
                    await self._create_in_tw(change, project_map)

    async def _push_tw_to_tt(self, change: SyncChange, project_map: dict) -> None:
        tt_fields = tw_task_to_ticktick(change.tw_task, change.mapping.ticktick_project)
        await self.tt.update_task(
            change.mapping.ticktick_id, change.mapping.ticktick_project, tt_fields
        )
        self._update_mapping_timestamps(change)

    def _resolve_tw_project(self, tt_task: dict, project_map: dict[str, str]) -> str | None:
        """Resolve a TickTick task's project to the mapped TaskWarrior project name.

        Returns None if no mapping exists, signalling the caller to skip this task.
        """
        tt_project_name = project_map.get(tt_task.get("projectId", ""), "")
        tw_project = self._tt_to_tw.get(tt_project_name)
        if tw_project is None:
            logger.warning(
                "No TW mapping for TickTick project %r (task %s) — skipping",
                tt_project_name, tt_task.get("id"),
            )
            return None
        return tw_project

    async def _push_tt_to_tw(self, change: SyncChange, project_map: dict) -> None:
        tw_project = self._resolve_tw_project(change.tt_task, project_map)
        if tw_project is None:
            return
        tw_fields = ticktick_task_to_tw(change.tt_task, tw_project)
        self.tw.update_task(str(change.tw_task["uuid"]), tw_fields)
        self._update_mapping_timestamps(change)

    async def _resolve_conflict(self, change: SyncChange, project_map: dict) -> None:
        tw_mod = change.tw_task.get("modified", "")
        tt_mod = change.tt_task.get("modifiedTime", "")
        if tw_mod >= tt_mod:
            await self._push_tw_to_tt(change, project_map)
        else:
            await self._push_tt_to_tw(change, project_map)

    async def _create_in_tt(self, change: SyncChange, project_map: dict) -> None:
        tw_project = change.tw_task.get("project", "")
        tt_project_name = self._tw_to_tt.get(tw_project)
        if tt_project_name is None:
            logger.debug("Skipping TW task %s — project %r has no mapping", change.tw_task["uuid"], tw_project)
            return

        # Resolve TickTick project name → project ID
        project_id = next(
            (pid for pid, name in project_map.items() if name == tt_project_name),
            None,
        )
        if project_id is None:
            logger.warning("TickTick project %r not found in API response — skipping", tt_project_name)
            return

        tt_fields = tw_task_to_ticktick(change.tw_task, project_id)
        created = await self.tt.create_task(tt_fields)
        self.store.upsert_mapping(TaskMapping(
            tw_uuid=str(change.tw_task["uuid"]),
            ticktick_id=created["id"],
            ticktick_project=created.get("projectId", project_id),
            last_sync_ts=time.time(),
            tw_modified=change.tw_task.get("modified"),
            ticktick_modified=created.get("modifiedTime"),
        ))

    async def _create_in_tw(self, change: SyncChange, project_map: dict) -> None:
        tw_project = self._resolve_tw_project(change.tt_task, project_map)
        if tw_project is None:
            return
        tw_fields = ticktick_task_to_tw(change.tt_task, tw_project)
        new_uuid = self.tw.create_task(tw_fields)
        self.store.upsert_mapping(TaskMapping(
            tw_uuid=new_uuid,
            ticktick_id=change.tt_task["id"],
            ticktick_project=change.tt_task.get("projectId", ""),
            last_sync_ts=time.time(),
            tw_modified=None,
            ticktick_modified=change.tt_task.get("modifiedTime"),
        ))

    def _update_mapping_timestamps(self, change: SyncChange) -> None:
        if not change.mapping:
            return
        change.mapping.last_sync_ts = time.time()
        change.mapping.tw_modified = change.tw_task.get("modified") if change.tw_task else None
        change.mapping.ticktick_modified = change.tt_task.get("modifiedTime") if change.tt_task else None
        self.store.upsert_mapping(change.mapping)
