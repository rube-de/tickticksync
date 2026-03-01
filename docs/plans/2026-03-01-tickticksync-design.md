# tickticksync Design

**Date:** 2026-03-01
**Status:** Approved
**Stack:** Python (uv), ticktick-sdk, tasklib, SQLite, Click

---

## Problem

No existing tool syncs TickTick and TaskWarrior bidirectionally. The closest candidate (`kgoettler/tasksync`) targets Todoist, not TickTick, and is abandoned. This project builds a purpose-built bidirectional sync daemon from scratch.

---

## Architecture

Two data paths running inside a single background daemon:

```
TaskWarrior  ──on-add/on-modify hooks──►  Unix socket  ──►  Daemon  ──►  TickTick API
                                                              │
                                          ◄── tasklib ◄──  Poll loop (every 60s)
```

### Push path (TW → TickTick)

TW hooks (`on-add`, `on-modify`) send task JSON to the daemon over a Unix socket and return immediately — no blocking. The daemon batches events with a 5-second idle window and flushes to the TickTick V1 API. If the socket is unavailable (daemon not running), events are written to a local queue file and replayed on next daemon start.

### Pull path (TickTick → TW)

A poll timer fires every 60 seconds (configurable). The daemon fetches all projects and tasks from the TickTick V1 API, diffs against last-known state in SQLite, and applies changes via `tasklib`.

---

## Project Structure

```
tickticksync/
├── pyproject.toml
├── src/tickticksync/
│   ├── cli.py          # Click CLI: init, sync, daemon, status
│   ├── config.py       # TOML config loading
│   ├── daemon.py       # asyncio event loop: socket listener + poll timer
│   ├── hooks.py        # TW hook entry points (on-add, on-modify)
│   ├── state.py        # SQLite state store
│   ├── sync.py         # core sync engine: conflict resolution + field mapping
│   ├── ticktick.py     # async wrapper over ticktick-sdk
│   └── taskwarrior.py  # tasklib wrapper
└── tests/
    ├── test_sync.py
    ├── test_mapping.py
    └── test_state.py
```

---

## Field Mapping

| TaskWarrior | TickTick V1 | Notes |
|---|---|---|
| `description` | `title` | Direct |
| `due` | `dueDate` | ISO 8601, timezone-aware |
| `project` | `projectId` → name | Bidirectional; inbox = default |
| `priority` (H/M/L/none) | `priority` (5/3/1/0) | |
| `status` (pending/completed/deleted) | `status` (0/2) + soft delete | |
| `annotations` | `content` | Joined as newlines |
| `ticktickid` UDA | `id` | Stable cross-system key |
| subtask `items[]` | TW annotations | `[x] item` / `[ ] item` format |

Tags are not synced — the TickTick V1 official API does not expose them.

---

## State Management

**SQLite database** at `~/.local/share/tickticksync/state.db`:

```sql
CREATE TABLE task_map (
    tw_uuid           TEXT PRIMARY KEY,
    ticktick_id       TEXT UNIQUE NOT NULL,
    ticktick_project  TEXT NOT NULL,
    last_sync_ts      REAL NOT NULL,
    tw_modified       TEXT,
    ticktick_modified TEXT
);

CREATE TABLE sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

`tw_modified` and `ticktick_modified` record field values at the time of last sync, enabling accurate change detection regardless of clock skew.

---

## Conflict Resolution

Last-write-wins based on modification timestamps:

1. Only TW changed since `last_sync_ts` → push to TickTick
2. Only TickTick changed → pull into TW
3. Both changed → compare `modified` (TW) vs `modifiedTime` (TickTick); newer wins
4. Neither changed → skip

**New tasks:**
- TickTick task not in `task_map` → create in TW, add to map
- TW task with no `ticktickid` UDA → create in TickTick, add to map

**Deletion:**
- TickTick `deleted=1` (soft delete) → `task delete` in TW
- TW `deleted` status → TickTick delete API call; filter `deleted=1` during polling to avoid re-creation loop

---

## Configuration

`~/.config/tickticksync/config.toml`:

```toml
[ticktick]
client_id     = "..."
client_secret = "..."
# OAuth token stored separately at ~/.config/tickticksync/token.json

[sync]
poll_interval = 60
batch_window  = 5
socket_path   = "/tmp/tickticksync.sock"
queue_path    = "~/.local/share/tickticksync/hook_queue.json"

[mapping]
default_project = "inbox"
```

---

## CLI

```
tickticksync init              # OAuth flow, register TW UDA, install hooks
tickticksync daemon start      # start background daemon
tickticksync daemon stop       # graceful shutdown
tickticksync daemon status     # running/stopped + last poll time
tickticksync sync              # one manual full sync cycle
tickticksync status            # mapped task count, last sync time, errors
```

`init` installs hook scripts into `~/.local/share/task/hooks/`:
- `on-add.tickticksync`
- `on-modify.tickticksync`

Hooks are minimal — write to socket (or queue file if socket unavailable) and exit.

---

## Dependencies

| Package | Purpose |
|---|---|
| `ticktick-sdk` (`dev-mirzabicer`) | TickTick V1 API client (async, Pydantic v2) |
| `tasklib` | TaskWarrior Python interface |
| `click` | CLI |
| `tomllib` (stdlib 3.11+) | Config parsing |
| `aiosqlite` | Async SQLite access |

Dev: `pytest`, `pytest-asyncio`, `respx` (mock httpx for TickTick API calls)

---

## Out of Scope

- TickTick tags (V1 API limitation)
- TickTick habits, focus/pomodoro
- Dida365 support (can be added later — same API, different host)
- Conflict logging / manual resolution UI
