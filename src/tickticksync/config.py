from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import tomllib
import tomlkit


DEFAULT_CONFIG_PATH = Path("~/.config/tickticksync/config.toml").expanduser()


@dataclass
class TickTickConfig:
    client_id: str
    client_secret: str


@dataclass
class SyncConfig:
    poll_interval: int = 60
    batch_window: int = 5
    socket_path: str = "/tmp/tickticksync.sock"
    queue_path: str = "~/.local/share/tickticksync/hook_queue.json"


@dataclass
class ProjectMapping:
    ticktick: str
    taskwarrior: str


@dataclass
class MappingConfig:
    default_project: str = "inbox"
    projects: list[ProjectMapping] = field(default_factory=list)


AuthMethod = Literal["oauth", "password"]
_VALID_AUTH_METHODS: set[str] = {"oauth", "password"}


@dataclass
class AuthConfig:
    method: AuthMethod = "oauth"
    username: str | None = None


@dataclass
class Config:
    ticktick: TickTickConfig
    sync: SyncConfig = field(default_factory=SyncConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)

    @property
    def token_path(self) -> Path:
        return Path("~/.config/tickticksync/token.json").expanduser()

    @property
    def db_path(self) -> Path:
        return Path("~/.local/share/tickticksync/state.db").expanduser()


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = DEFAULT_CONFIG_PATH
    with open(path, "rb") as f:
        data = tomllib.load(f)
    auth_data = data.get("auth", {})
    method = auth_data.get("method", "oauth")
    if method not in _VALID_AUTH_METHODS:
        raise ValueError(
            f"Invalid auth.method {method!r} in config; expected one of {_VALID_AUTH_METHODS}"
        )
    mapping_data = data.get("mapping", {})
    projects_raw = mapping_data.pop("projects", [])
    projects: list[ProjectMapping] = []
    for i, p in enumerate(projects_raw):
        try:
            projects.append(ProjectMapping(**p))
        except TypeError as e:
            raise ValueError(
                f"Invalid mapping.projects[{i}] entry in config; expected keys "
                f"'ticktick' and 'taskwarrior'; got {p!r}"
            ) from e
    return Config(
        ticktick=TickTickConfig(**data["ticktick"]),
        sync=SyncConfig(**data.get("sync", {})),
        mapping=MappingConfig(**mapping_data, projects=projects),
        auth=AuthConfig(**auth_data),
    )


def save_config_auth(path: Path, method: AuthMethod, username: str | None = None) -> None:
    """Write or overwrite the [auth] section in the config file at *path*.

    Uses tomlkit to preserve existing formatting in other sections.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    doc = tomlkit.parse(text)
    auth_table = tomlkit.table()
    auth_table.add("method", method)
    if username is not None:
        auth_table.add("username", username)
    doc["auth"] = auth_table
    path.write_text(tomlkit.dumps(doc))


def save_config_mapping(path: Path, projects: list[ProjectMapping]) -> None:
    """Write or overwrite [[mapping.projects]] in config, preserving other sections."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    doc = tomlkit.parse(text)
    if "mapping" not in doc:
        doc.add("mapping", tomlkit.table())
    aot = tomlkit.aot()
    for pm in projects:
        t = tomlkit.table()
        t.add("ticktick", pm.ticktick)
        t.add("taskwarrior", pm.taskwarrior)
        aot.append(t)
    doc["mapping"]["projects"] = aot
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def save_config_full(
    path: Path,
    *,
    client_id: str,
    client_secret: str,
    auth_method: AuthMethod,
    poll_interval: int,
    socket_path: str,
    projects: list[ProjectMapping],
    auth_username: str | None = None,
) -> None:
    """Write a complete config file with all sections."""
    doc = tomlkit.document()

    tt_table = tomlkit.table()
    tt_table.add("client_id", client_id)
    tt_table.add("client_secret", client_secret)
    doc.add("ticktick", tt_table)

    auth_table = tomlkit.table()
    auth_table.add("method", auth_method)
    if auth_username is not None:
        auth_table.add("username", auth_username)
    doc.add("auth", auth_table)

    sync_table = tomlkit.table()
    sync_table.add("poll_interval", poll_interval)
    sync_table.add("batch_window", SyncConfig.batch_window)
    sync_table.add("socket_path", socket_path)
    sync_table.add("queue_path", SyncConfig.queue_path)
    doc.add("sync", sync_table)

    mapping_table = tomlkit.table()
    if projects:
        aot = tomlkit.aot()
        for pm in projects:
            t = tomlkit.table()
            t.add("ticktick", pm.ticktick)
            t.add("taskwarrior", pm.taskwarrior)
            aot.append(t)
        mapping_table.add("projects", aot)
    doc.add("mapping", mapping_table)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(path)
