# tests/test_config.py
import pytest
from pathlib import Path
from tickticksync.config import load_config, save_config_auth, save_config_mapping, Config, SyncConfig, MappingConfig, ProjectMapping


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


def test_save_config_auth_oauth(tmp_config):
    save_config_auth(tmp_config, "oauth")
    cfg = load_config(tmp_config)
    assert cfg.auth.method == "oauth"
    assert cfg.auth.username is None


def test_save_config_auth_password_with_username(tmp_config):
    save_config_auth(tmp_config, "password", "user@example.com")
    cfg = load_config(tmp_config)
    assert cfg.auth.method == "password"
    assert cfg.auth.username == "user@example.com"


def test_save_config_auth_preserves_other_sections(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[ticktick]\nclient_id = "id"\nclient_secret = "secret"\n\n'
        "[sync]\npoll_interval = 120\n"
    )
    save_config_auth(cfg_path, "password", "me@example.com")
    cfg = load_config(cfg_path)
    assert cfg.sync.poll_interval == 120
    assert cfg.auth.method == "password"
    assert cfg.auth.username == "me@example.com"


def test_save_config_auth_creates_auth_from_scratch(tmp_path):
    cfg_path = tmp_path / "missing.toml"
    save_config_auth(cfg_path, "oauth")
    text = cfg_path.read_text()
    assert "oauth" in text


def test_load_config_with_auth_section(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[ticktick]\nclient_id = "id"\nclient_secret = "secret"\n\n'
        '[auth]\nmethod = "password"\nusername = "u@e.com"\n'
    )
    cfg = load_config(cfg_path)
    assert cfg.auth.method == "password"
    assert cfg.auth.username == "u@e.com"


def test_load_config_invalid_auth_method(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[ticktick]\nclient_id = "id"\nclient_secret = "secret"\n\n'
        '[auth]\nmethod = "oath"\n'
    )
    with pytest.raises(ValueError, match="Invalid auth.method"):
        load_config(cfg_path)


def test_project_mapping_fields():
    pm = ProjectMapping(ticktick="Inbox", taskwarrior="inbox")
    assert pm.ticktick == "Inbox"
    assert pm.taskwarrior == "inbox"


def test_mapping_config_projects_default():
    mc = MappingConfig()
    assert mc.projects == []
    assert isinstance(mc.projects, list)


def test_load_config_with_project_mappings(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""\
[ticktick]
client_id = "id"
client_secret = "secret"

[mapping]
default_project = "inbox"

[[mapping.projects]]
ticktick = "Inbox"
taskwarrior = "inbox"

[[mapping.projects]]
ticktick = "Work"
taskwarrior = "work"
""")
    cfg = load_config(cfg_path)
    assert len(cfg.mapping.projects) == 2
    assert cfg.mapping.projects[0].ticktick == "Inbox"
    assert cfg.mapping.projects[0].taskwarrior == "inbox"
    assert cfg.mapping.projects[1].ticktick == "Work"
    assert cfg.mapping.projects[1].taskwarrior == "work"


def test_load_config_without_project_mappings(tmp_config):
    cfg = load_config(tmp_config)
    assert cfg.mapping.projects == []


def test_save_config_mapping_roundtrip(tmp_config):
    projects = [
        ProjectMapping(ticktick="Inbox", taskwarrior="inbox"),
        ProjectMapping(ticktick="Work", taskwarrior="work"),
    ]
    save_config_mapping(tmp_config, projects)
    cfg = load_config(tmp_config)
    assert len(cfg.mapping.projects) == 2
    assert cfg.mapping.projects[0].ticktick == "Inbox"
    assert cfg.mapping.projects[1].taskwarrior == "work"


def test_save_config_mapping_preserves_other_sections(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""\
[ticktick]
client_id = "id"
client_secret = "secret"

[sync]
poll_interval = 120

[auth]
method = "password"
username = "me@example.com"
""")
    projects = [ProjectMapping(ticktick="Inbox", taskwarrior="inbox")]
    save_config_mapping(cfg_path, projects)
    cfg = load_config(cfg_path)
    assert cfg.ticktick.client_id == "id"
    assert cfg.sync.poll_interval == 120
    assert cfg.auth.method == "password"
    assert cfg.auth.username == "me@example.com"
    assert len(cfg.mapping.projects) == 1


def test_save_config_mapping_empty_list(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""\
[ticktick]
client_id = "id"
client_secret = "secret"

[[mapping.projects]]
ticktick = "Old"
taskwarrior = "old"
""")
    save_config_mapping(cfg_path, [])
    cfg = load_config(cfg_path)
    assert cfg.mapping.projects == []


def test_save_config_mapping_overwrites_existing(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""\
[ticktick]
client_id = "id"
client_secret = "secret"

[[mapping.projects]]
ticktick = "Old"
taskwarrior = "old"
""")
    projects = [ProjectMapping(ticktick="New", taskwarrior="new")]
    save_config_mapping(cfg_path, projects)
    cfg = load_config(cfg_path)
    assert len(cfg.mapping.projects) == 1
    assert cfg.mapping.projects[0].ticktick == "New"


def test_load_config_duplicate_project_names(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""\
[ticktick]
client_id = "id"
client_secret = "secret"

[[mapping.projects]]
ticktick = "Inbox"
taskwarrior = "inbox"

[[mapping.projects]]
ticktick = "Inbox"
taskwarrior = "inbox_dup"
""")
    cfg = load_config(cfg_path)
    assert len(cfg.mapping.projects) == 2


def test_save_config_mapping_creates_from_scratch(tmp_path):
    cfg_path = tmp_path / "new.toml"
    projects = [ProjectMapping(ticktick="Inbox", taskwarrior="inbox")]
    save_config_mapping(cfg_path, projects)
    text = cfg_path.read_text()
    assert "ticktick" in text
    assert "taskwarrior" in text
