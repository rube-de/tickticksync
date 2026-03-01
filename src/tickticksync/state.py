import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TaskMapping:
    tw_uuid: str
    ticktick_id: str
    ticktick_project: str
    last_sync_ts: float
    tw_modified: Optional[str] = None
    ticktick_modified: Optional[str] = None


class StateStore:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_map (
                tw_uuid           TEXT PRIMARY KEY,
                ticktick_id       TEXT UNIQUE NOT NULL,
                ticktick_project  TEXT NOT NULL,
                last_sync_ts      REAL NOT NULL,
                tw_modified       TEXT,
                ticktick_modified TEXT
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self._conn.commit()

    def upsert_mapping(self, m: TaskMapping) -> None:
        self._conn.execute(
            """
            INSERT INTO task_map VALUES (?,?,?,?,?,?)
            ON CONFLICT(tw_uuid) DO UPDATE SET
                ticktick_id       = excluded.ticktick_id,
                ticktick_project  = excluded.ticktick_project,
                last_sync_ts      = excluded.last_sync_ts,
                tw_modified       = excluded.tw_modified,
                ticktick_modified = excluded.ticktick_modified
            """,
            (m.tw_uuid, m.ticktick_id, m.ticktick_project,
             m.last_sync_ts, m.tw_modified, m.ticktick_modified),
        )
        self._conn.commit()

    def get_by_tw_uuid(self, tw_uuid: str) -> Optional[TaskMapping]:
        row = self._conn.execute(
            "SELECT * FROM task_map WHERE tw_uuid=?", (tw_uuid,)
        ).fetchone()
        return TaskMapping(*row) if row else None

    def get_by_ticktick_id(self, ticktick_id: str) -> Optional[TaskMapping]:
        row = self._conn.execute(
            "SELECT * FROM task_map WHERE ticktick_id=?", (ticktick_id,)
        ).fetchone()
        return TaskMapping(*row) if row else None

    def all_mappings(self) -> list[TaskMapping]:
        rows = self._conn.execute("SELECT * FROM task_map").fetchall()
        return [TaskMapping(*r) for r in rows]

    def delete_by_tw_uuid(self, tw_uuid: str) -> None:
        self._conn.execute("DELETE FROM task_map WHERE tw_uuid=?", (tw_uuid,))
        self._conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO sync_state VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
