import pytest
from pathlib import Path


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[ticktick]
client_id = "test_id"
client_secret = "test_secret"
""")
    return cfg
