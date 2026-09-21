"""Execute real save/route bodies without booting cameras or discovery services."""
import ast
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace, MethodType
import uuid
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, request

from InferenceNode.auth import db
from InferenceNode.auth.models import Base
from InferenceNode import node_settings_store as nss, publisher_store as pst
import InferenceNode.data_models
from InferenceNode.log_manager import LogManager


def body(name, node):
    tree = ast.parse(Path('InferenceNode/inference_node.py').read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    fn.decorator_list = []
    namespace = dict(self=node, jsonify=jsonify, request=request, os=os, tempfile=tempfile, uuid=uuid)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), '<node body>', 'exec'), namespace)
    return namespace[name]


@pytest.fixture
def env(tmp_path):
    db._engine = None
    db._SessionLocal = None
    engine = db.init_engine(f'sqlite:///{tmp_path / "settings.db"}')
    Base.metadata.create_all(engine)
    yield
    engine.dispose()
    db._engine = None
    db._SessionLocal = None


def node():
    log = Mock()
    log.get_settings.return_value = {'log_level': 'WARNING', 'file_logging_enabled': False,
                                    'max_log_size_mb': 23, 'retention_days': 8}
    n = SimpleNamespace(node_id='node-id', node_name='new-name', port=5000,
                        node_info={}, discovery_manager=None, log_manager=log,
                        result_publisher=SimpleNamespace(destinations=[]), telemetry=None,
                        logger=logging.getLogger('settings-test'))
    n._save_settings = MethodType(body('_save_settings', n), n)
    return n


def test_settings_write_rolls_back_all_rows_and_raises(env, monkeypatch):
    nss.set_setting(nss.KEY_NODE_IDENTITY, {'node_name': 'old-name'})
    n = node()
    monkeypatch.setattr(pst, 'list_publishers', Mock(side_effect=RuntimeError('database failure')))
    with pytest.raises(RuntimeError, match='database failure'):
        n._save_settings()
    assert nss.get_setting(nss.KEY_NODE_IDENTITY)['node_name'] == 'old-name'
    assert nss.get_setting(nss.KEY_PREFERENCES) is None


def test_logging_settings_persist_and_restore_on_new_node(env):
    n = node()
    n._save_settings()
    saved = nss.get_setting(nss.KEY_PREFERENCES)['logging']
    fresh = node()
    fresh._load_settings_dict = lambda: {'logging': saved, 'publishers': []}
    MethodType(body('_load_settings', fresh), fresh)()
    fresh.log_manager.update_settings.assert_called_once_with({
        'log_level': 'WARNING', 'enable_file_logging': False,
        'max_log_size_mb': 23, 'retention_days': 8})


@pytest.mark.parametrize('route,payload', [
    ('update_node_config', {'node_name': 'attempted-name'}),
    ('update_log_settings', {'log_level': 'ERROR'}),
])
def test_settings_api_does_not_report_success_on_database_failure(env, monkeypatch, route, payload):
    n = node()
    n.log_manager.update_settings.return_value = True
    monkeypatch.setattr(nss, 'set_setting', Mock(side_effect=RuntimeError('write failed')))
    app = Flask(__name__)
    app.add_url_rule('/save', view_func=body(route, n), methods=['POST'])
    result = app.test_client().post('/save', json=payload)
    assert result.status_code == 500
    assert not result.json.get('success')
    assert n.node_name == 'new-name'


def test_publisher_description_survives_create_update_and_clear(env):
    p = pst.create_publisher(name='favorite', type='null', config={}, description='first description')
    assert pst.get_publisher(p['id'])['description'] == 'first description'
    pst.update_publisher(p['id'], description='edited')
    assert pst.list_publishers()[0]['description'] == 'edited'
    pst.update_publisher(p['id'], name='renamed')
    assert pst.get_publisher(p['id'])['description'] == 'edited'
    pst.update_publisher(p['id'], description='')
    assert pst.get_publisher(p['id'])['description'] == ''


def test_favorite_routes_round_trip_description(env):
    n = node()
    app = Flask(__name__)
    app.add_url_rule('/favorites', view_func=body('save_favorite_config', n), methods=['POST'])
    app.add_url_rule('/favorites/<favorite_id>', view_func=body('update_favorite_config', n), methods=['PUT'])
    c = app.test_client()
    created = c.post('/favorites', json={'name': 'fav', 'type': 'null', 'config': {}, 'description': 'notes'})
    assert created.status_code == 200
    pid = created.json['favorite']['id']
    assert created.json['favorite']['description'] == 'notes'
    changed = c.put('/favorites/' + pid, json={'description': 'updated notes'})
    assert changed.status_code == 200
    assert pst.get_publisher(pid)['description'] == 'updated notes'


def test_same_name_concurrent_uploads_keep_distinct_bytes_and_cleanup():
    paths = []
    barrier = threading.Barrier(2)
    def store(path, *args, **kwargs):
        paths.append(path)
        barrier.wait(timeout=10)
        return Path(path).read_bytes().decode()
    n = node()
    n.model_repo = SimpleNamespace(store_model=store)
    app = Flask(__name__)
    app.add_url_rule('/upload', view_func=body('upload_model', n), methods=['POST'])
    def upload(payload):
        with app.test_client() as c:
            r = c.post('/upload', data={'file': (io.BytesIO(payload.encode()), 'same.pt')})
            assert r.status_code == 200
            return r.json['model_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(upload, ['first', 'second'])) == ['first', 'second']
    assert len(set(paths)) == 2
    assert all(not Path(p).parent.exists() for p in paths)


def test_failed_upload_cleans_staging_directory():
    paths = []
    def store(path, *args, **kwargs):
        paths.append(path)
        raise RuntimeError('invalid model')
    n = node()
    n.model_repo = SimpleNamespace(store_model=store)
    app = Flask(__name__)
    app.add_url_rule('/upload', view_func=body('upload_model', n), methods=['POST'])
    r = app.test_client().post('/upload', data={'file': (io.BytesIO(b'bad'), 'model.onnx')})
    assert r.status_code == 500
    assert paths and not Path(paths[0]).parent.exists()


def test_changed_port_is_rejected_before_mutation():
    n = node()
    app = Flask(__name__)
    app.add_url_rule('/save', view_func=body('update_node_config', n), methods=['POST'])
    r = app.test_client().post('/save', json={'web_port': 8000, 'node_name': 'changed'})
    assert r.status_code == 400
    assert n.node_name == 'new-name'


def test_rotation_size_applies_to_existing_handler():
    manager = LogManager()
    manager.file_handler = Mock()
    assert manager.update_settings({'max_log_size_mb': 17})
    assert manager.file_handler.maxBytes == 17 * 1024 * 1024
    assert not manager.update_settings({'max_log_size_mb': -1})
    assert manager.file_handler.maxBytes == 17 * 1024 * 1024


def test_failed_late_publisher_write_rolls_back_prior_changes(env, monkeypatch):
    nss.set_setting(nss.KEY_NODE_IDENTITY, {'node_name': 'old'})
    n = node()
    n.result_publisher.destinations = [SimpleNamespace(_id='one'), SimpleNamespace(_id='two')]
    original = pst.create_publisher
    def create(**kwargs):
        if kwargs['publisher_id'] == 'two':
            raise RuntimeError('second publisher failed')
        return original(**kwargs)
    monkeypatch.setattr(pst, 'create_publisher', create)
    with pytest.raises(RuntimeError, match='second publisher failed'):
        n._save_settings()
    assert pst.list_publishers() == []
    assert nss.get_setting(nss.KEY_NODE_IDENTITY)['node_name'] == 'old'


def test_missing_encryption_key_fails_whole_save(env, monkeypatch):
    from InferenceNode import config_secrets
    monkeypatch.setattr(config_secrets, 'encrypt_config', Mock(side_effect=config_secrets.SecretsUnavailable('key missing')))
    with pytest.raises(config_secrets.SecretsUnavailable):
        node()._save_settings()
    assert nss.get_setting(nss.KEY_NODE_IDENTITY) is None


def test_telemetry_api_propagates_save_failure(env):
    n = node()
    n.telemetry = SimpleNamespace(update_interval=30, running=False,
                                  start_telemetry=Mock(), stop_telemetry=Mock())
    n._save_settings = Mock(side_effect=RuntimeError('commit failed'))
    app = Flask(__name__)
    app.add_url_rule('/telemetry', view_func=body('configure_telemetry', n), methods=['POST'])
    r = app.test_client().post('/telemetry', json={'enabled': False, 'mqtt_server': ''})
    assert r.status_code == 500
    assert r.json['error'] == 'commit failed'


def test_node_publisher_api_propagates_save_failure():
    n = node()
    n.result_publisher.add = Mock(return_value='destination-id')
    n._save_settings = Mock(side_effect=RuntimeError('commit failed'))
    route = body('configure_publisher', n)
    destination = Mock(is_configured=True)
    route.__globals__['ResultDestination'] = Mock(return_value=destination)
    app = Flask(__name__)
    app.add_url_rule('/publisher', view_func=route, methods=['POST'])
    r = app.test_client().post('/publisher', json={'type': 'null', 'config': {}})
    assert r.status_code == 500
    assert r.json['error'] == 'commit failed'


def test_blank_broker_settings_restore_after_restart():
    n = node()
    n.telemetry = SimpleNamespace(update_interval=30, running=False,
        mqtt_server=None, mqtt_port=1883, mqtt_topic=None, stop_telemetry=Mock())
    n._load_settings_dict = lambda: {'telemetry': {'enabled': False, 'publish_interval': 17,
        'mqtt_server': '', 'mqtt_port': 1885, 'mqtt_topic': 'saved/topic'}, 'publishers': []}
    MethodType(body('_load_settings', n), n)()
    assert (n.telemetry.mqtt_server, n.telemetry.mqtt_port, n.telemetry.mqtt_topic) == ('',1885,'saved/topic')
    assert n.telemetry.update_interval == 17
