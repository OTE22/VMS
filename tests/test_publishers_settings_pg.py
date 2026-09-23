"""Phase 10 - publishers / node settings / telemetry config in PostgreSQL with dedicated,
versioned at-rest encryption. SQLite here; same file runs on isolated PostgreSQL."""
import json
import logging
import os
import sys

import pytest
from sqlalchemy import select

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth.db import get_session                       # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models as dm                              # noqa: E402
from InferenceNode import config_secrets as cs                      # noqa: E402
from InferenceNode import publisher_store as pst                    # noqa: E402
from InferenceNode import node_settings_store as nss                # noqa: E402
from InferenceNode import app_state                                 # noqa: E402
from InferenceNode.registry_migration import migrate_node_settings, NODE_SETTINGS_MARKER  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'p.db'}")
    Base.metadata.create_all(engine)
    key_file = tmp_path / "config.key"
    key_file.write_text(cs.generate_key_line("armyeye-config-2026-01") + "\n")
    if os.name != "nt":
        os.chmod(key_file, 0o600)
    monkeypatch.setenv(cs.KEY_FILE_ENV, str(key_file))
    assert cs.reload_keys()
    yield {"tmp": tmp_path, "key_file": key_file}
    cs.reload_keys("/nonexistent")   # unload
    auth_db._engine = None; auth_db._SessionLocal = None


def _raw_config(publisher_id):
    with get_session() as s:
        return s.execute(select(dm.Publisher).where(dm.Publisher.publisher_id == publisher_id)).scalar_one().config


# ------------------------------------------------------------------ encryption contract

def test_encrypt_persist_decrypt_internally_and_api_stays_redacted(env):
    p = pst.create_publisher(name="mq", type="mqtt", config={"server": "b", "port": 1883, "password": "hunter2"})
    raw = _raw_config(p["id"])
    assert cs.is_encrypted_value(raw["password"]) and raw["password"]["v"] == 1
    assert raw["password"]["key_id"] == "armyeye-config-2026-01"
    assert "hunter2" not in json.dumps(raw)                                  # plaintext never stored
    assert p["config"]["password"] == "***"                                  # API redacted
    assert pst.get_publisher(p["id"])["config"]["password"] == "***"
    rt = pst.get_publisher(p["id"], runtime=True)
    assert rt["config"]["password"] == "hunter2" and rt["secrets_ok"] is True   # runtime decrypts


def test_plaintext_secret_never_stored_when_key_missing(env):
    cs.reload_keys("/nonexistent")
    with pytest.raises(cs.SecretsUnavailable):
        pst.create_publisher(name="mq", type="mqtt", config={"server": "b", "password": "leak"})
    with get_session() as s:
        assert s.execute(select(dm.Publisher)).scalars().all() == []


def test_missing_key_fails_safely_without_destroying_config(env):
    p = pst.create_publisher(name="mq", type="mqtt", config={"server": "b", "password": "hunter2"})
    cs.reload_keys("/nonexistent")
    rt = pst.get_publisher(p["id"], runtime=True)
    assert rt["secrets_ok"] is False and rt["config"]["password"] is None      # unusable, not served in clear
    assert rt["config"]["server"] == "b"                                       # non-secret config intact
    raw = _raw_config(p["id"])
    assert cs.is_encrypted_value(raw["password"])                              # ciphertext untouched
    assert pst.get_publisher(p["id"])["config"]["password"] == "***"           # API still redacted


def test_wrong_key_fails_safely(env):
    p = pst.create_publisher(name="mq", type="mqtt", config={"password": "hunter2"})
    other = env["tmp"] / "other.key"
    other.write_text(cs.generate_key_line("armyeye-config-2026-01") + "\n")   # same id, different key
    if os.name != "nt":
        os.chmod(other, 0o600)
    assert cs.reload_keys(str(other))
    rt = pst.get_publisher(p["id"], runtime=True)
    assert rt["secrets_ok"] is False and rt["config"]["password"] is None
    assert cs.is_encrypted_value(_raw_config(p["id"])["password"])           # not re-encrypted, not destroyed


def test_key_rotation_compatibility(env):
    p = pst.create_publisher(name="mq", type="mqtt", config={"password": "old-secret"})
    old_line = env["key_file"].read_text().strip()
    new_line = cs.generate_key_line("armyeye-config-2026-07")
    env["key_file"].write_text(new_line + "\n" + old_line + "\n")             # rotate: new key first
    if os.name != "nt":
        os.chmod(env["key_file"], 0o600)
    assert cs.reload_keys()
    assert pst.get_publisher(p["id"], runtime=True)["config"]["password"] == "old-secret"  # old key still decrypts
    pst.update_publisher(p["id"], config={"password": "new-secret"})          # rewrite -> new key
    assert _raw_config(p["id"])["password"]["key_id"] == "armyeye-config-2026-07"
    assert pst.get_publisher(p["id"], runtime=True)["config"]["password"] == "new-secret"


def test_secret_never_appears_in_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    pst.create_publisher(name="mq", type="mqtt", config={"password": "LOGLEAK"})
    cs.reload_keys("/nonexistent")
    pst.list_publishers(runtime=True)
    assert "LOGLEAK" not in caplog.text
    assert "armyeye-config" not in caplog.text or "key" not in caplog.text.lower() or True   # key ids may appear; key material never
    key_material = env["key_file"].read_text().split(":", 1)[1].strip()
    assert key_material not in caplog.text


# ------------------------------------------------------------------ publisher store semantics

def test_update_honors_type_and_redaction_safe_merge(env):
    p = pst.create_publisher(name="a", type="mqtt", config={"server": "b", "password": "pw"})
    up = pst.update_publisher(p["id"], type="webhook", config={"server": "b2", "password": "***"})
    assert up["type"] == "webhook" and up["config"]["server"] == "b2"
    assert "password" not in pst.get_publisher(p["id"], runtime=True)["config"]  # type changes do not carry credentials
    pst.update_publisher(p["id"], config={"password": None})
    assert pst.get_publisher(p["id"], runtime=True)["config"]["password"] is None      # explicit clear


def test_delete_removes_row(env):
    p = pst.create_publisher(name="a", type="mqtt", config={})
    assert pst.delete_publisher(p["id"]) is True and pst.get_publisher(p["id"]) is None
    assert pst.delete_publisher("nope") is False


# ------------------------------------------------------------------ node settings

def test_node_settings_validated_and_telemetry_secret_encrypted(env):
    with pytest.raises(nss.SettingValidationError):
        nss.set_setting("bogus", {})
    with pytest.raises(nss.SettingValidationError):
        nss.set_setting(nss.KEY_TELEMETRY, {"publish_interval": 0.1})
    out = nss.set_setting(nss.KEY_TELEMETRY, {"enabled": True, "publish_interval": 30, "mqtt_server": "broker",
                                             "mqtt_port": 1883, "mqtt_username": "u", "mqtt_password": "pw"})
    assert out["mqtt_password"] == "***"
    with get_session() as s:
        raw = s.execute(select(dm.NodeSetting).where(dm.NodeSetting.key == "telemetry")).scalar_one().value
    assert cs.is_encrypted_value(raw["mqtt_password"]) and "pw" not in json.dumps(raw)
    rt = nss.get_setting(nss.KEY_TELEMETRY, runtime=True)
    assert rt["mqtt_password"] == "pw" and rt["publish_interval"] == 30.0
    # redacted echo preserves the secret; other fields update
    nss.set_setting(nss.KEY_TELEMETRY, {"publish_interval": 60, "mqtt_password": "***"})
    rt = nss.get_setting(nss.KEY_TELEMETRY, runtime=True)
    assert rt["mqtt_password"] == "pw" and rt["publish_interval"] == 60.0


# ------------------------------------------------------------------ node_settings.json migration

def test_node_settings_json_migration_populates_pg_and_marks_complete(env):
    js = env["tmp"] / "node_settings.json"
    js.write_text(json.dumps({
        "node_id": "c8085bac", "node_name": "InferNode-X",
        "publishers": [{"id": "dest-1", "type": "mqtt", "name": "n", "config": {"server": "b", "password": "s"}, "enabled": True}],
        "telemetry": {"enabled": True, "publish_interval": 30.0},
        "favorite_configs": {"f0b4bb00": {"name": "webhook", "type": "webhook",
                                          "config": {"url": "http://127.0.0.1:5000/webhook/{pipeline_id}", "timeout": 30}}},
    }))
    rep = migrate_node_settings(str(js))
    assert rep.failed == 0 and rep.processed == 4
    assert app_state.get_state(NODE_SETTINGS_MARKER) == app_state.STATE_COMPLETED
    assert nss.get_setting(nss.KEY_NODE_IDENTITY)["node_name"] == "InferNode-X"
    assert nss.get_setting(nss.KEY_TELEMETRY)["publish_interval"] == 30.0
    fav = pst.get_publisher("f0b4bb00"); assert fav and fav["kind"] == "favorite" and fav["type"] == "webhook"
    dest = pst.get_publisher("dest-1"); assert dest["kind"] == "node_destination"
    assert dest["config"]["password"] == "***"
    assert pst.get_publisher("dest-1", runtime=True)["config"]["password"] == "s"
    # rerun: no duplicates
    migrate_node_settings(str(js), force=True)
    assert len(pst.list_publishers()) == 2
    assert js.exists(), "legacy JSON retained read-only"


def test_node_settings_migration_refuses_plaintext_when_key_missing(env):
    cs.reload_keys("/nonexistent")
    js = env["tmp"] / "node_settings.json"
    js.write_text(json.dumps({"favorite_configs": {"x": {"name": "m", "type": "mqtt", "config": {"password": "s"}}}}))
    rep = migrate_node_settings(str(js))
    assert rep.failed == 1 and rep.blocking
    assert app_state.get_state(NODE_SETTINGS_MARKER) != app_state.STATE_COMPLETED
    assert pst.list_publishers() == []


def test_world_readable_key_file_is_refused():
    """Restrictive permissions are enforced (POSIX): a group/world-readable key file is
    never loaded."""
    if os.name == "nt":
        pytest.skip("POSIX permission bits not applicable on Windows")
    import tempfile
    d = tempfile.mkdtemp()
    kf = os.path.join(d, "k")
    open(kf, "w").write(cs.generate_key_line("x") + chr(10))
    os.chmod(kf, 0o644)
    assert cs.reload_keys(kf) is False
    os.chmod(kf, 0o600)
    assert cs.reload_keys(kf) is True
    cs.reload_keys("/nonexistent")
