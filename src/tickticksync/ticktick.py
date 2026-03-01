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
    ) -> None:
        token_file = Path(token_path)
        stored_token: str | None = None
        if token_file.exists():
            try:
                data = json.loads(token_file.read_text())
                stored_token = data.get("access_token")
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read token file: %s", token_path)

        effective_token = v1_access_token or stored_token
        self._real = _RealTickTickClient(
            client_id=client_id,
            client_secret=client_secret,
            v1_access_token=effective_token,
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
        # The real SDK create_task takes keyword args; unpack the fields dict.
        title = fields.pop("title", "")
        project_id = fields.pop("projectId", None) or fields.pop("project_id", None)
        task = await self._real.create_task(title, project_id, **fields)
        return _to_dict(task)

    async def update_task(
        self, task_id: str, project_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a task identified by *task_id* / *project_id* with *fields*."""
        task = _sdk.Task(id=task_id, projectId=project_id, **fields)
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

    def __init__(self, client_id: str, client_secret: str, token_path: str) -> None:
        self._client = TickTickClient(client_id, client_secret, token_path)

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

        Returns:
            A 2-tuple of:
            - tasks: list of task dicts with ``deleted != 1`` entries removed.
            - project_map: mapping of ``project_id -> project_name``.
        """
        projects = await self._client.get_projects()
        project_map: dict[str, str] = {p["id"]: p["name"] for p in projects}

        all_tasks: list[dict[str, Any]] = []
        for project in projects:
            project_id: str = project["id"]
            try:
                data = await self._client.get_project_data(project_id)
                tasks = data.get("tasks", [])
                # Filter soft-deleted tasks (deleted == 1).
                active = [t for t in tasks if not t.get("deleted")]
                all_tasks.extend(active)
            except Exception:
                logger.exception(
                    "Failed to fetch tasks for project %s (%s)",
                    project.get("name"),
                    project_id,
                )

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
