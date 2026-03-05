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

This will:
1. Prompt for your TickTick OAuth `client_id` and `client_secret`
2. Write config to `~/.config/tickticksync/config.toml`
3. Register the `ticktickid` UDA in TaskWarrior
4. Install `on-add` and `on-modify` hook scripts

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

## Configuration

Config lives at `~/.config/tickticksync/config.toml`:

```toml
[ticktick]
client_id = "your-client-id"
client_secret = "your-client-secret"

[sync]
poll_interval = 60          # seconds between full syncs
socket_path = "/tmp/tickticksync.sock"

[mapping]
default_project = "inbox"   # TickTick project for new TW tasks
```

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
