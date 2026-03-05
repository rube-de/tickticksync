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
    return Config(
        ticktick=TickTickConfig(**data["ticktick"]),
        sync=SyncConfig(**data.get("sync", {})),
        mapping=MappingConfig(**data.get("mapping", {})),
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
