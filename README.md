# tickticksync

Bidirectional sync between [TickTick](https://ticktick.com) and [TaskWarrior](https://taskwarrior.org).

A background daemon listens for TaskWarrior hook events (push path) and polls the TickTick API on a timer (pull path). SQLite tracks task mappings and last-known state for last-write-wins conflict resolution.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- TaskWarrior installed and configured

## Install

### Global install (recommended for CLI usage)

```bash
# Using uv (recommended)
uv tool install tickticksync

# Using pipx
pipx install tickticksync
```

### Project dependency

```bash
# Using uv
uv add tickticksync

# Using pip
pip install tickticksync
```

### From GitHub (latest development version)

```bash
uv pip install git+https://github.com/rube-de/tickticksync.git
```

### Development

```bash
git clone https://github.com/rube-de/tickticksync.git
cd tickticksync
uv sync --dev
```

## Setup

```bash
tickticksync init
```

The `init` command runs an interactive 4-step wizard:

1. **Credentials** — prompts for your TickTick OAuth `client_id` and `client_secret`
2. **OAuth authentication** — opens your browser to complete the OAuth flow and saves the access token
3. **Sync settings** — configures poll interval and socket path (sensible defaults provided)
4. **Project mappings** — fetches your TickTick projects from the API and lets you map each one to a TaskWarrior project

The wizard also registers the `ticktickid` UDA in TaskWarrior and installs `on-add`/`on-modify` hook scripts.

If config already exists at `~/.config/tickticksync/config.toml`, the wizard asks whether to reconfigure or just re-run the idempotent setup steps (UDA + hooks).

## Usage

### One-off sync

```bash
tickticksync sync
```

### Daemon

```bash
tickticksync daemon start   # start background sync
tickticksync daemon stop    # stop the daemon
tickticksync daemon status  # check if running
```

### Status

```bash
tickticksync status         # show mapped task count and last sync time
```

### Authentication

```bash
tickticksync auth oauth     # re-run the browser OAuth2 flow
tickticksync auth password  # store username/password credentials in the system keyring
```

### Managing mappings

```bash
tickticksync mapping list                                        # show all project mappings
tickticksync mapping add                                         # interactive: pick from TickTick projects
tickticksync mapping add --ticktick "Work" --taskwarrior "work"  # non-interactive
tickticksync mapping remove "Work"                               # remove by TickTick project name
```

Example `mapping list` output:

```
TickTick Project  → TaskWarrior Project
───────────────────────────────────────
Work              → work
Personal          → personal
(2 mappings)
```

## Mapping

tickticksync uses explicit project mappings to control which TickTick projects sync with TaskWarrior. Each mapping pairs one TickTick project with one TaskWarrior project.

**Key behavior:**
- Only mapped projects are synced — unmapped TickTick projects are ignored
- Mappings are one-to-one: each TickTick project maps to exactly one TaskWarrior project (and vice versa)
- Tasks that move to an unmapped project are skipped during sync

Mappings are set up during `tickticksync init` (step 4) or managed afterward with `tickticksync mapping add/remove`.

## Configuration

Config lives at `~/.config/tickticksync/config.toml`:

```toml
[ticktick]
client_id = "your-client-id"
client_secret = "your-client-secret"

[auth]
method = "oauth"                    # "oauth" (default) or "password"

[sync]
poll_interval = 60                  # seconds between full syncs
batch_window = 5                    # reserved — not yet active
socket_path = "/tmp/tickticksync.sock"
queue_path = "/home/you/.local/share/tickticksync/hook_queue.json"  # use absolute path; ~ is not expanded

[mapping]
default_project = "inbox"           # reserved — not yet active

[[mapping.projects]]
ticktick = "Work"
taskwarrior = "work"

[[mapping.projects]]
ticktick = "Personal"
taskwarrior = "personal"
```

| Section | Key | Description |
|---------|-----|-------------|
| `[ticktick]` | `client_id`, `client_secret` | TickTick OAuth app credentials |
| `[auth]` | `method` | Authentication method: `"oauth"` or `"password"` |
| `[auth]` | `username` | TickTick username (email) for `"password"` auth method |
| `[sync]` | `poll_interval` | Seconds between daemon sync cycles |
| `[sync]` | `batch_window` | Seconds to batch hook events before syncing (reserved — not yet active) |
| `[sync]` | `socket_path` | Unix socket path for hook-to-daemon communication |
| `[sync]` | `queue_path` | File path for queued events when daemon is down (use absolute path; `~` is not expanded) |
| `[mapping]` | `default_project` | Fallback TickTick project for unmapped TW tasks (reserved — not yet active) |
| `[[mapping.projects]]` | `ticktick`, `taskwarrior` | One-to-one project mapping pair |

## Architecture

```
TaskWarrior hooks ──► Unix socket ──► Daemon ──► SyncEngine
                                        ▲
                              poll timer ┘
                                        │
                              TickTick API (concurrent project fetches)
                                        │
                              SQLite state store (task mappings)
```

- **Push path**: TW `on-add`/`on-modify` hooks send task JSON to the daemon via Unix socket. If the daemon is down, events queue to disk and replay on next startup.
- **Pull path**: The daemon polls TickTick on a configurable interval, fetching all projects concurrently.
- **Conflict resolution**: Last-write-wins based on modification timestamps.
- **Field mapping**: Priority levels, due dates, annotations, and checklist items are mapped between the two systems.

## Development

```bash
uv sync --dev
uv run pytest
```

## License

MIT
