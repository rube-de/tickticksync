from dataclasses import dataclass, field
from pathlib import Path
import tomllib
import tomlkit


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
class MappingConfig:
    default_project: str = "inbox"


@dataclass
class AuthConfig:
    method: str = "oauth"
    username: str = ""


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
        path = Path("~/.config/tickticksync/config.toml").expanduser()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config(
        ticktick=TickTickConfig(**data["ticktick"]),
        sync=SyncConfig(**data.get("sync", {})),
        mapping=MappingConfig(**data.get("mapping", {})),
        auth=AuthConfig(**data.get("auth", {})),
    )


def save_config_auth(path: Path, method: str, username: str = "") -> None:
    """Write or overwrite the [auth] section in the config file at *path*.

    Uses tomlkit to preserve existing formatting in other sections.
    """
    text = path.read_text() if path.exists() else ""
    doc = tomlkit.parse(text)
    auth_table = tomlkit.table()
    auth_table.add("method", method)
    if username:
        auth_table.add("username", username)
    doc["auth"] = auth_table
    path.write_text(tomlkit.dumps(doc))
