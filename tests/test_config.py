# tests/test_config.py
import pytest
from pathlib import Path
from tickticksync.config import load_config, Config, SyncConfig, MappingConfig


def test_load_minimal_config(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.ticktick.client_id == "test_id"
    assert cfg.ticktick.client_secret == "test_secret"


def test_sync_defaults(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.sync.poll_interval == 60
    assert cfg.sync.batch_window == 5
    assert cfg.sync.socket_path == "/tmp/tickticksync.sock"


def test_mapping_defaults(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.mapping.default_project == "inbox"


def test_missing_client_id_raises(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[ticktick]\nclient_secret = 'x'\n")
    with pytest.raises((KeyError, TypeError)):
        load_config(bad)


def test_custom_poll_interval(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[ticktick]
client_id = "id"
client_secret = "secret"
[sync]
poll_interval = 30
""")
    cfg = load_config(cfg_path)
    assert cfg.sync.poll_interval == 30
