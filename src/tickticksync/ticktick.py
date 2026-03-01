"""TickTick API client wrapper.

Provides a thin adapter over the ticktick-sdk TickTickClient that:
- Uses method names consistent with this project's internal conventions.
- Returns plain dicts rather than Pydantic models so the rest of the sync
  engine stays SDK-agnostic.
- Separates authentication lifecycle from individual API calls so callers
  can share a single connected client across the daemon loop.

SDK: ticktick-sdk==0.4.3
Real import: from ticktick_sdk import TickTickClient as _RealTickTickClient
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import ticktick_sdk as _sdk
from ticktick_sdk import TickTickClient as _RealTickTickClient

logger = logging.getLogger(__name__)


def _to_dict(obj: Any) -> dict[str, Any] | Any:
    """Convert a Pydantic model to a plain dict, or pass through if already dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, exclude_none=False)
    return obj


class TickTickClient:
    """Adapter around the real SDK TickTickClient.

    Exposes a stable method surface with plain-dict return values.  This class
    is the patch target in tests (``tickticksync.ticktick.TickTickClient``).

    In production the class wraps ``ticktick_sdk.TickTickClient`` and manages
    its async lifecycle via ``connect()`` / ``disconnect()``.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: str,
        *,
        v1_access_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        stored_token: str | None = None
        try:
            data = json.loads(Path(token_path).read_text())
            stored_token = data.get("access_token")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            logger.debug("No usable token file at %s", token_path)

        effective_token = v1_access_token or stored_token
        self._real = _RealTickTickClient(
            client_id=client_id,
            client_secret=client_secret,
            v1_access_token=effective_token,
            username=username,
            password=password,
        )
        self._token_path = token_path

    async def connect(self) -> None:
        """Authenticate and open the underlying HTTP session."""
        await self._real.connect()

    async def disconnect(self) -> None:
        """Close the underlying HTTP session."""
        await self._real.disconnect()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def get_projects(self) -> list[dict[str, Any]]:
        """Return all projects as plain dicts."""
        projects = await self._real.get_all_projects()
        return [_to_dict(p) for p in projects]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def get_all_tasks_v2(self) -> list[dict[str, Any]]:
        """Return all tasks via V2 (session) API as plain dicts."""
        tasks = await self._real.get_all_tasks()
        return [_to_dict(t) for t in tasks]

    async def get_project_data(self, project_id: str) -> dict[str, Any]:
        """Return project data (tasks + columns) for *project_id* as a plain dict."""
        data = await self._real.get_project_tasks(project_id)
        result = _to_dict(data)
        # Normalise: ensure the "tasks" key always exists and contains dicts.
        if "tasks" not in result:
            result["tasks"] = []
        else:
            result["tasks"] = [_to_dict(t) for t in result["tasks"]]
        return result

    async def create_task(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a task from a plain-dict payload and return the created task."""
        fields = dict(fields)  # prevent mutating caller's dict
        title = fields.pop("title", "")
        project_id = fields.pop("projectId", None) or fields.pop("project_id", None)
        # Only pass title and project_id; the SDK's create_task does not accept
        # camelCase keys (dueDate, startDate, etc.) that callers may include.
        # TODO: follow up with update_task if additional fields need to be set.
        task = await self._real.create_task(title=title, project_id=project_id)
        return _to_dict(task)

    async def update_task(
        self, task_id: str, project_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a task identified by *task_id* / *project_id* with *fields*."""
        # Strip id/projectId from fields to avoid duplicate-keyword TypeError
        # when constructing the Task model (those values come from the explicit args).
        safe_fields = {
            k: v
            for k, v in fields.items()
            if k not in ("id", "projectId", "project_id")
        }
        task = _sdk.Task(id=task_id, projectId=project_id, **safe_fields)
        updated = await self._real.update_task(task)
        return _to_dict(updated)

    async def delete_task(self, task_id: str, project_id: str) -> None:
        """Permanently delete a task."""
        await self._real.delete_task(task_id, project_id)

    async def complete_task(self, task_id: str, project_id: str) -> None:
        """Mark a task as complete."""
        await self._real.complete_task(task_id, project_id)


class TickTickAPI:
    """Public API surface for the sync engine.

    Instantiates a :class:`TickTickClient` (patchable in tests) and delegates
    all calls through it.  The engine should call :meth:`connect` before use
    and :meth:`disconnect` on shutdown.

    Args:
        client_id: OAuth2 client ID from the TickTick developer portal.
        client_secret: OAuth2 client secret.
        token_path: Path to a JSON file where the OAuth token is persisted.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._client = TickTickClient(
            client_id, client_secret, token_path,
            username=username, password=password,
        )
        self._use_v2_tasks = username is not None

    async def connect(self) -> None:
        """Authenticate with TickTick."""
        await self._client.connect()

    async def disconnect(self) -> None:
        """Release the HTTP session."""
        await self._client.disconnect()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get_projects(self) -> list[dict[str, Any]]:
        """Return all TickTick projects as plain dicts."""
        return await self._client.get_projects()

    async def get_all_tasks(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Fetch every non-deleted task across all projects.

        Uses V2 (session) API when password auth is configured — a single
        ``get_all_tasks()`` call.  Falls back to per-project V1 fetches for
        OAuth auth.

        Returns:
            A 2-tuple of:
            - tasks: list of task dicts with ``deleted != 1`` entries removed.
            - project_map: mapping of ``project_id -> project_name``.
        """
        if self._use_v2_tasks:
            return await self._get_all_tasks_v2()
        return await self._get_all_tasks_v1()

    async def _get_all_tasks_v2(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """V2 path: tasks and projects fetched concurrently, then merged."""
        raw_tasks, projects = await asyncio.gather(
            self._client.get_all_tasks_v2(),
            self._client.get_projects(),
        )
        project_map: dict[str, str] = {p["id"]: p["name"] for p in projects}
        tasks = [t for t in raw_tasks if not t.get("deleted")]
        return tasks, project_map

    async def _get_all_tasks_v1(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """V1 path: per-project fetches (original implementation)."""
        projects = await self._client.get_projects()
        project_map: dict[str, str] = {p["id"]: p["name"] for p in projects}

        async def _fetch_project(project: dict) -> list[dict[str, Any]]:
            try:
                data = await self._client.get_project_data(project["id"])
                return [t for t in data.get("tasks", []) if not t.get("deleted")]
            except Exception:
                logger.exception(
                    "Failed to fetch tasks for project %s (%s)",
                    project.get("name"),
                    project["id"],
                )
                return []

        results = await asyncio.gather(*(_fetch_project(p) for p in projects))
        all_tasks = [t for batch in results for t in batch]
        return all_tasks, project_map

    async def create_task(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a new task from *fields* and return the created task dict."""
        return await self._client.create_task(fields)

    async def update_task(
        self, task_id: str, project_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Update task *task_id* in *project_id* with *fields*."""
        return await self._client.update_task(task_id, project_id, fields)

    async def delete_task(self, task_id: str, project_id: str) -> None:
        """Permanently delete task *task_id* from *project_id*."""
        await self._client.delete_task(task_id, project_id)

    async def complete_task(self, task_id: str, project_id: str) -> None:
        """Mark task *task_id* in *project_id* as complete."""
        await self._client.complete_task(task_id, project_id)
