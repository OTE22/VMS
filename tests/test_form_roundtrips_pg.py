"""Deep form audit: actual Flask routes + CSRF + isolated migrated PostgreSQL.

Regression coverage for the audited form contracts.
No cameras, external model downloads, broker connections or production data.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid

import pytest
from sqlalchemy import text
from InferenceNode.auth.service import verify_password

def check_password_hash(hashed, plain):
    return verify_password(plain, hashed)


@pytest.fixture(scope='module')
def live_forms(isolated_pg, tmp_path_factory):
    from InferenceNode.auth import db
    from InferenceNode import config_secrets
    from InferenceNode.inference_node import InferenceNode
    tmp = tmp_path_factory.mktemp('form-audit')
    key = tmp / 'key'
    key.write_text(config_secrets.generate_key_line('form-audit') + '\n')
    key.chmod(0o600)
    patch = pytest.MonkeyPatch()
    for k, v in {
        'ARMYEYE_DATABASE_URL': isolated_pg['url'], 'ARMYEYE_ARTIFACT_ROOT': isolated_pg['artifact_root'],
        'ARMYEYE_LEGACY_ROOT': str(tmp / 'empty-legacy'), 'ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE': str(key),
        'ADMIN_USERNAME': 'form_audit_admin', 'ADMIN_PASSWORD': 'Audit-Initial-Password-1!',
        'FLASK_SECRET_KEY': 'isolated-form-audit-session-key', 'ENABLE_ENGINE_BUILDER': '1',
    }.items():
        patch.setenv(k, v)
    # This network-isolated container shares PostgreSQL's network namespace;
    # resolve only its own hostname locally for psutil's Node Info response.
    import socket
    original_lookup = socket.gethostbyname
    patch.setattr(socket, 'gethostbyname', lambda h: '127.0.0.1' if h == socket.gethostname() else original_lookup(h))
    db._engine = None; db._SessionLocal = None
    node = InferenceNode(node_name='form-audit', port=5999, legacy_root=str(tmp / 'empty-legacy'))
    app = node.app
    client = app.test_client()
    def token(page):
        m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
        if not m:
            m = re.search(r'name="csrf-token"[^>]*content="([^"]+)"', page)
        assert m, 'CSRF token missing'
        return m.group(1)
    csrf = token(client.get('/login').get_data(as_text=True))
    r = client.post('/login', data={'username': 'form_audit_admin', 'password': 'Audit-Initial-Password-1!', 'csrf_token': csrf})
    assert r.status_code == 302
    if 'change-password' in r.location:
        csrf = token(client.get('/change-password').get_data(as_text=True))
        r = client.post('/change-password', data={'current_password': 'Audit-Initial-Password-1!',
            'new_password': 'Audit-New-Password-2!', 'confirm_password': 'Audit-New-Password-2!', 'csrf_token': csrf})
        assert r.status_code == 302
    csrf = token(client.get('/').get_data(as_text=True))
    def send(path, payload=None, method='POST', **kwargs):
        return client.open(path, method=method, json=payload, headers={'X-CSRFToken': csrf}, **kwargs)
    with isolated_pg['engine'].connect() as c:
        assert c.execute(text('select current_database()')).scalar_one() == isolated_pg['dbname']
    yield {'client': client, 'send': send, 'engine': isolated_pg['engine'], 'node': node,
           'root': Path(isolated_pg['artifact_root']), 'token': token, 'csrf': csrf}
    if node.telemetry:
        node.telemetry.stop_telemetry()
    import logging
    node.log_manager._detach_own_handlers(logging.getLogger())
    patch.undo()
    config_secrets.reload_keys('/nonexistent')
    db._engine = None; db._SessionLocal = None


def one(env, sql, **params):
    with env['engine'].connect() as c:
        return c.execute(text(sql), params).mappings().one()


def model_checkpoint_bytes(marker='audit'):
    import zipfile
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('archive/data.pkl', b'\x80\x02}q\x00.')
        archive.writestr('archive/version', '3')
        archive.writestr('archive/audit.txt', marker)
    return stream.getvalue()


def model(env):
    name = 'audit-' + uuid.uuid4().hex[:8]
    content = model_checkpoint_bytes(name)
    r = env['client'].post('/api/models/upload', headers={'X-CSRFToken': env['csrf']}, data={
        'file': (io.BytesIO(content), 'same.pt'), 'engine_type': 'ultralytics',
        'name': name, 'description': 'Model form description'})
    assert r.status_code == 200, r.json
    return r.json['model_id'], name, content


def pipeline(env, mid=None):
    if mid is None:
        mid = model(env)[0]
    payload = {'name': 'Pipeline ' + uuid.uuid4().hex[:8], 'description': 'Description filled',
        'inference_enabled': False,
        'frame_source': {'capture_type': 'ip_camera', 'config': {'source': 'rtsp://camera.invalid/live',
            'username': 'audit-user', 'password': 'audit-camera-secret', 'buffer_size': 0}},
        'model': {'id': mid or '', 'engine_type': 'ultralytics' if mid else 'pass', 'device': 'cpu'},
        'destinations': [{'type': 'null', 'enabled': False, 'config': {}}]}
    r = env['send']('/api/pipeline/create', payload)
    assert r.status_code == 200, r.json
    return r.json['pipeline_id'], payload


def test_model_upload_fields_and_artifact_hash(live_forms):
    mid, name, content = model(live_forms)
    row = one(live_forms, 'SELECT * FROM models WHERE model_id=:mid', mid=mid)
    assert row['name'] == name
    # Registry metadata and API are checked separately from the byte artifact.
    got = live_forms['client'].get('/api/models/' + mid).json
    assert 'Model form description' in json.dumps(got)
    art = one(live_forms, 'SELECT a.* FROM model_artifacts a JOIN models m ON m.id=a.model_id WHERE m.model_id=:mid', mid=mid)
    assert art['sha256'] == hashlib.sha256(content).hexdigest()
    assert (live_forms['root'] / 'models' / art['relative_path']).read_bytes() == content


def test_media_upload_registry_and_canonical_reference(live_forms):
    data = playable_video_bytes()
    r = live_forms['client'].post('/api/media/upload-video', headers={'X-CSRFToken': live_forms['csrf']},
        data={'file': (io.BytesIO(data), 'audit clip.mp4')})
    assert r.status_code == 200, r.json
    row = one(live_forms, 'SELECT * FROM media_assets WHERE media_id=:id', id=r.json['media_id'])
    assert r.json['relative_source'] == row['relative_path']
    assert row['original_filename'] == 'audit clip.mp4'
    assert row['sha256'] == hashlib.sha256(data).hexdigest()


def test_pipeline_form_create_edit_and_redacted_read(live_forms):
    pid, payload = pipeline(live_forms)
    row = one(live_forms, 'SELECT * FROM pipelines WHERE pipeline_id=:pid', pid=pid)
    assert row['name'] == payload['name'] and row['description'] == payload['description']
    assert row['config']['inference_enabled'] is False
    assert row['config']['destinations'][0]['enabled'] is False
    assert row['config']['frame_source']['config']['buffer_size'] == 0
    assert 'audit-camera-secret' not in json.dumps(row['config'])
    from InferenceNode.pipeline_repository import repository
    assert repository.get(pid)['config']['frame_source']['config']['password'] == 'audit-camera-secret'
    r = live_forms['client'].get('/api/pipeline/' + pid)
    assert r.status_code == 200 and 'audit-camera-secret' not in r.get_data(as_text=True)
    payload['name'] = 'Edited'; payload['description'] = ''
    payload['frame_source']['config']['password'] = '***'
    assert live_forms['send']('/api/pipeline/' + pid, payload, method='PUT').status_code == 200
    row = one(live_forms, 'SELECT * FROM pipelines WHERE pipeline_id=:pid', pid=pid)
    assert row['name'] == 'Edited' and row['description'] == ''
    assert 'audit-camera-secret' not in json.dumps(row['config'])
    from InferenceNode.pipeline_repository import repository
    assert repository.get(pid)['config']['frame_source']['config']['password'] == 'audit-camera-secret'


def test_switching_pipeline_to_pass_clears_old_model(live_forms):
    mid, _, _ = model(live_forms)
    pid, payload = pipeline(live_forms, mid)
    payload['model'] = {'id': '', 'engine_type': 'pass', 'device': 'cpu'}
    assert live_forms['send']('/api/pipeline/' + pid, payload, method='PUT').status_code == 200
    row = one(live_forms, 'SELECT model_id,config FROM pipelines WHERE pipeline_id=:pid', pid=pid)
    assert row['model_id'] is None
    assert not row['config']['model']['id']


def test_publisher_form_fields_encryption_edit_and_clear(live_forms):
    payload = {'name': 'Favorite ' + uuid.uuid4().hex[:8], 'description': 'Description filled',
        'type': 'mqtt', 'config': {'server': 'broker.invalid', 'port': 1883, 'topic': 'audit',
        'password': 'audit-mqtt-secret', 'include_image_data': False}}
    r = live_forms['send']('/api/publisher/favorites', payload)
    assert r.status_code == 200, r.json
    pid = r.json['favorite']['id']
    row = one(live_forms, 'SELECT * FROM publishers WHERE publisher_id=:id', id=pid)
    assert row['name'] == payload['name'] and row['description'] == payload['description']
    assert row['config']['port'] == 1883 and row['config']['include_image_data'] is False
    assert 'audit-mqtt-secret' not in json.dumps(row['config'])
    assert r.json['favorite']['config']['password'] == '***'
    payload['description'] = ''; payload['config']['password'] = '***'
    r = live_forms['send']('/api/publisher/favorites/' + pid, payload, method='PUT')
    assert r.status_code == 200 and r.json['favorite']['description'] == ''
    from InferenceNode import publisher_store
    assert publisher_store.get_publisher(pid, runtime=True)['config']['password'] == 'audit-mqtt-secret'
    # Backend supports explicit null clearing; Publisher's collector does not emit it.
    assert live_forms['send']('/api/publisher/favorites/' + pid, {'config': {'password': None}}, method='PUT').status_code == 200
    assert publisher_store.get_publisher(pid, runtime=True)['config']['password'] is None


def test_node_and_log_forms_database_and_get(live_forms):
    r = live_forms['send']('/api/node/config', {'node_name': 'Audit node edited', 'log_level': 'WARNING', 'web_port': 5999})
    assert r.status_code == 200, r.json
    row = one(live_forms, "SELECT value FROM node_settings WHERE key='node_identity'")
    assert row['value']['node_name'] == 'Audit node edited'
    got = live_forms['client'].get('/api/node/info').json['data']['config']
    assert got['node_name'] == 'Audit node edited' and got['log_level'] == 'WARNING'
    values = {'log_level': 'ERROR', 'max_log_size_mb': 21, 'retention_days': 12, 'enable_file_logging': False}
    assert live_forms['send']('/api/logs/settings', values).status_code == 200
    row = one(live_forms, "SELECT value FROM node_settings WHERE key='preferences'")
    expected = dict(values); expected['file_logging_enabled'] = expected.pop('enable_file_logging')
    assert row['value']['logging'] == expected
    assert live_forms['client'].get('/api/logs/settings').json['settings'] == expected


def test_telemetry_form_without_broker_round_trips_all_fields(live_forms):
    payload = {'enabled': False, 'publish_interval': 17, 'mqtt_server': '', 'mqtt_port': 1885, 'mqtt_topic': 'new/topic'}
    r = live_forms['send']('/api/telemetry/configure', payload)
    assert r.status_code == 200, r.json
    row = one(live_forms, "SELECT value FROM node_settings WHERE key='telemetry'")
    assert row['value']['publish_interval'] == 17 and row['value']['enabled'] is False
    assert row['value']['mqtt_port'] == 1885 and row['value']['mqtt_topic'] == 'new/topic'


def test_admin_create_edit_reset_password_and_access(live_forms):
    name = 'audit_user_' + uuid.uuid4().hex[:7]
    payload = {'username': name, 'full_name': 'Audit Full Name', 'email': 'audit@example.invalid',
        'password': 'Audit-User-Password-1!', 'role': 'user', 'must_change_password': True}
    r = live_forms['send']('/api/users', payload)
    assert r.status_code == 201, r.json
    uid = r.json['user']['id']
    row = one(live_forms, 'SELECT * FROM users WHERE id=:id', id=uid)
    for field in ('username', 'full_name', 'email', 'role', 'must_change_password'):
        assert row[field] == payload[field]
    assert row['password_hash'] != payload['password'] and check_password_hash(row['password_hash'], payload['password'])
    assert 'password_hash' not in r.json['user']
    r = live_forms['send']('/api/users/' + str(uid), {'email': '', 'full_name': ''}, method='PATCH')
    assert r.status_code == 200
    row = one(live_forms, 'SELECT * FROM users WHERE id=:id', id=uid)
    assert row['email'] is None and row['full_name'] is None
    version = row['permissions_version']
    r = live_forms['send'](f'/api/users/{uid}/reset-password', {'password': 'Audit-Reset-Password-2!', 'must_change': False})
    assert r.status_code == 200
    row = one(live_forms, 'SELECT * FROM users WHERE id=:id', id=uid)
    assert check_password_hash(row['password_hash'], 'Audit-Reset-Password-2!')
    assert row['permissions_version'] > version and row['must_change_password'] is False
    pid, _ = pipeline(live_forms)
    r = live_forms['send'](f'/api/pipelines/{pid}/access/{uid}', {'can_view': True, 'can_start': True, 'can_stop': False, 'can_edit': False}, method='PUT')
    assert r.status_code == 200, r.json
    row = one(live_forms, 'SELECT a.* FROM pipeline_user_access a JOIN pipelines p ON p.id=a.pipeline_id WHERE p.pipeline_id=:pid AND a.user_id=:uid', pid=pid, uid=uid)
    assert row['can_view'] and row['can_start'] and not row['can_stop'] and not row['can_edit']


def test_login_change_password_logout_html_forms(live_forms):
    name = 'audit_login_' + uuid.uuid4().hex[:7]
    initial = 'Audit-Login-Password-1!'; changed = 'Audit-Changed-Password-2!'
    r = live_forms['send']('/api/users', {'username': name, 'password': initial, 'role': 'user', 'must_change_password': True})
    assert r.status_code == 201
    uid = r.json['user']['id']
    c = live_forms['node'].app.test_client()
    token = live_forms['token'](c.get('/login').get_data(as_text=True))
    assert c.post('/login', data={'username': name, 'password': initial, 'csrf_token': token}).status_code == 302
    token = live_forms['token'](c.get('/change-password').get_data(as_text=True))
    assert c.post('/change-password', data={'current_password': initial, 'new_password': changed, 'confirm_password': changed, 'csrf_token': token}).status_code == 302
    row = one(live_forms, 'SELECT * FROM users WHERE id=:id', id=uid)
    assert check_password_hash(row['password_hash'], changed) and not row['must_change_password']
    assert row['last_login'] is not None
    assert c.post('/logout', data={'csrf_token': token}).status_code == 302
    assert c.get('/api/pipelines').status_code == 401


def test_missing_csrf_does_not_create_favorite(live_forms):
    before = one(live_forms, 'SELECT count(*) AS n FROM publishers')['n']
    r = live_forms['client'].post('/api/publisher/favorites', json={'name': 'must-not-save', 'type': 'null', 'config': {}})
    assert r.status_code == 400
    assert one(live_forms, 'SELECT count(*) AS n FROM publishers')['n'] == before


def test_engine_wizard_fields_generated_installed_and_registered(live_forms):
    fields = {'display_name': 'Audit Engine ' + uuid.uuid4().hex[:6], 'task': 'detection',
              'confidence': 0.63, 'extensions': ['.onnx'], 'draw_color': [12, 34, 56], 'class_map': {'0': 'person'}}
    payload = {'preset': 'blank', 'fields': fields}
    r = live_forms['send']('/api/inference/engines/preview', payload)
    assert r.status_code == 200, r.json
    code = r.json['code']
    assert '0.63' in code and '.onnx' in code
    assert live_forms['send']('/api/inference/engines/validate', {'code': code}).json['valid'] is True
    r = live_forms['send']('/api/inference/engines', payload)
    assert r.status_code == 201, r.json
    key = r.json['engine']['engine_key']
    row = one(live_forms, 'SELECT * FROM inference_engines WHERE engine_key=:key', key=key)
    assert row['status'] == 'AVAILABLE' and row['validation_status'] == 'PASSED'
    path = live_forms['root'] / 'engines' / row['relative_path']
    # Registry paths may already include their artifact-kind prefix.
    if not path.exists(): path = live_forms['root'] / row['relative_path']
    assert path.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']


def test_new_pass_pipeline_exact_browser_payload(live_forms):
    pipeline(live_forms, '')


def test_export_registered_model_pipeline(live_forms):
    pid, _ = pipeline(live_forms)
    r = live_forms['client'].get(f'/api/pipeline/{pid}/export')
    assert r.status_code == 200, r.json
    assert r.mimetype == 'application/zip'


def test_import_pipeline_with_model_bytes(live_forms):
    import zipfile
    payload = {'name': 'Imported audit', 'description': 'Imported description',
        'frame_source': {'capture_type': 'webcam', 'config': {'source': 0}},
        'model': {'id': 'old-model-id', 'engine_type': 'ultralytics', 'device': 'cpu'},
        'inference_enabled': False, 'destinations': [{'type': 'null', 'config': {}}]}
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as z:
        z.writestr('pipeline_config.json', json.dumps(payload))
        z.writestr('models/model.pt', b'AUDIT MODEL BYTES')
    content.seek(0)
    r = live_forms['client'].post('/api/pipeline/import', headers={'X-CSRFToken': live_forms['csrf']},
                                  data={'file': (content, 'audit.zip')})
    assert r.status_code == 200, r.json


def test_saved_webhook_headers_are_usable_by_destination(live_forms):
    from ResultPublisher import ResultDestination
    from InferenceNode import publisher_store
    payload = {'name': 'Audit webhook', 'description': '', 'type': 'webhook',
               'config': {'url': 'http://127.0.0.1:9/audit', 'headers': 'X-Audit: value', 'timeout': 30}}
    r = live_forms['send']('/api/publisher/favorites', payload)
    assert r.status_code == 200, r.json
    stored = publisher_store.get_publisher(r.json['favorite']['id'], runtime=True)
    dest = ResultDestination('webhook')
    try:
        dest.configure(**stored['config'])
    finally:
        dest.stop_queue()


def test_import_config_without_model_artifact(live_forms):
    import zipfile
    payload = {'name': 'Config import audit', 'description': 'Imported description',
        'frame_source': {'capture_type': 'webcam', 'config': {'source': 0}},
        'model': {'id': None, 'engine_type': 'pass', 'device': 'cpu'},
        'inference_enabled': False, 'destinations': [{'type': 'null', 'config': {}}]}
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as z: z.writestr('pipeline_config.json', json.dumps(payload))
    content.seek(0)
    r = live_forms['client'].post('/api/pipeline/import', headers={'X-CSRFToken': live_forms['csrf']}, data={'file': (content, 'audit.zip')})
    assert r.status_code == 200, r.json
    row = one(live_forms, 'SELECT * FROM pipelines WHERE pipeline_id=:pid', pid=r.json['pipeline_id'])
    assert row['description'] == 'Imported description' and row['config']['inference_enabled'] is False


def test_export_import_preserves_model_bytes_and_disabled_inference(live_forms):
    import zipfile
    mid, _, content = model(live_forms)
    pid, _ = pipeline(live_forms, mid)
    exported = live_forms['client'].get(f'/api/pipeline/{pid}/export')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.data)) as z:
        assert json.loads(z.read('pipeline_config.json'))['inference_enabled'] is False
    response = live_forms['client'].post('/api/pipeline/import', headers={'X-CSRFToken': live_forms['csrf']},
        data={'file': (io.BytesIO(exported.data), '../../audit.zip')})
    assert response.status_code == 200, response.json
    new_mid = response.json['model_id']
    assert new_mid != mid
    assert Path(live_forms['node'].model_repo.get_model_path(new_mid)).read_bytes() == content
    assert Path(live_forms['node'].model_repo.get_model_path(mid)).read_bytes() == content


def test_failed_import_removes_new_model(live_forms, monkeypatch):
    pid, _ = pipeline(live_forms)
    exported = live_forms['client'].get(f'/api/pipeline/{pid}/export')
    before = one(live_forms, 'SELECT count(*) AS n FROM models')['n']
    from InferenceNode import pipeline_store
    def fail(*args, **kwargs):
        raise RuntimeError('simulated pipeline commit failure')
    monkeypatch.setattr(pipeline_store, 'create_pipeline', fail)
    response = live_forms['client'].post('/api/pipeline/import', headers={'X-CSRFToken': live_forms['csrf']},
        data={'file': (io.BytesIO(exported.data), 'audit.zip')})
    assert response.status_code == 500
    assert one(live_forms, 'SELECT count(*) AS n FROM models')['n'] == before


def test_test_message_reports_each_favorite(live_forms):
    ids = []
    for i in range(2):
        r = live_forms['send']('/api/publisher/favorites', {'name': f'Audit null {i}', 'type':'null', 'config':{}})
        assert r.status_code == 200
        ids.append(r.json['favorite']['id'])
    r = live_forms['send']('/api/publisher/test-favorites', {'message':{'test':True}, 'favorite_ids':ids})
    assert r.status_code == 200, r.json
    assert set(r.json['results']) == set(ids)
    assert all(result['status'] == 'success' for result in r.json['results'].values())


# Pipeline Builder safety regressions (shared isolated authenticated fixture).

def builder_config():
    return {'name': 'Builder '+uuid.uuid4().hex, 'frame_source': {'capture_type':'webcam','config':{'source':0}},
            'model': {'engine_type':'pass','device':'cpu'}, 'destinations':[], 'inference_enabled':False}


def builder_saved(env, response):
    assert response.status_code == 200, response.json
    from InferenceNode.pipeline_repository import repository
    return repository.get(response.json['pipeline_id'])['config']


@pytest.mark.parametrize('source', [
    {'capture_type':'ip_camera','config':{}},
    {'capture_type':'audit_unknown','config':{}},
    {'capture_type':'ip_camera','config':None},
])
def test_invalid_source_not_saved(live_forms, source):
    body=builder_config();body['frame_source']=source
    response=live_forms['send']('/api/pipeline/create',body)
    assert response.status_code == 400, response.json
    assert one(live_forms,'SELECT count(*) AS n FROM pipelines WHERE name=:name',name=body['name'])['n']==0


def test_favorite_secret_resolved_without_api_disclosure(live_forms):
    favorite=live_forms['send']('/api/publisher/favorites',{'name':uuid.uuid4().hex,'type':'mqtt',
        'config':{'server':'broker.invalid','port':1883,'topic':'test','password':'fake-favorite-secret'}}).json['favorite']
    assert favorite['config']['password']=='***'
    body=builder_config();body['destinations']=[{'type':'mqtt','favorite_id':favorite['id'],'config':favorite['config']}]
    result=builder_saved(live_forms,live_forms['send']('/api/pipeline/create',body))
    assert result['destinations'][0]['config']['password']=='fake-favorite-secret'
    assert 'favorite_id' not in result['destinations'][0]
    assert 'fake-favorite-secret' not in live_forms['client'].get('/api/pipelines').get_data(as_text=True)
    body['destinations'][0]['config']['password']=None
    result=builder_saved(live_forms,live_forms['send']('/api/pipeline/create',body))
    assert result['destinations'][0]['config']['password'] is None


def test_camera_test_inherits_secret_and_keeps_inference_disabled(live_forms):
    body=builder_config();body['frame_source']={'capture_type':'ip_camera','config':{'source':'rtsp://camera.invalid/live','password':'fake-camera-secret'}}
    original=builder_saved(live_forms,live_forms['send']('/api/pipeline/create',body))
    body['source_pipeline_id']=original['id'];body['frame_source']['config']['password']='***'
    result=builder_saved(live_forms,live_forms['send']('/api/pipeline/create',body))
    assert result['frame_source']['config']['password']=='fake-camera-secret'
    assert result['inference_enabled'] is False
    assert 'source_pipeline_id' not in result
    body['source_pipeline_id']=str(uuid.uuid4())
    assert live_forms['send']('/api/pipeline/create',body).status_code==404


def test_invalid_edit_preserves_saved_configuration(live_forms):
    original=builder_saved(live_forms,live_forms['send']('/api/pipeline/create',builder_config()))
    response=live_forms['send']('/api/pipeline/'+original['id'],{'frame_source':{'capture_type':'ip_camera','config':{}}},method='PUT')
    assert response.status_code==400
    assert one(live_forms,'SELECT config FROM pipelines WHERE pipeline_id=:pid',pid=original['id'])['config']['frame_source']==original['frame_source']


# Management controls: real PostgreSQL transactions with isolated fake runtime.
def management_pipeline(env):
    body = builder_config()
    body['inference_enabled'] = True
    body['destinations'] = [{'type': 'null', 'enabled': True, 'config': {}}]
    return builder_saved(env, env['send']('/api/pipeline/create', body))


def test_management_missing_publisher_is_not_success(live_forms):
    p = management_pipeline(live_forms)
    response = live_forms['send']('/api/pipeline/'+p['id']+'/publisher/'+str(uuid.uuid4())+'/disable')
    assert response.status_code == 404
    stored = one(live_forms, 'SELECT config FROM pipelines WHERE pipeline_id=:pid', pid=p['id'])['config']
    assert stored['destinations'] == p['destinations']


def test_management_concurrent_workers_preserve_both_controls(live_forms, monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from InferenceNode.pipeline_manager import PipelineManager
    p = management_pipeline(live_forms)
    first = live_forms['node'].pipeline_manager
    second = PipelineManager(str(tmp_path), store=first.store)
    original = first.store.set_control
    barrier = threading.Barrier(2)
    def concurrent_write(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)
    monkeypatch.setattr(first.store, 'set_control', concurrent_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.disable_pipeline_inference, p['id'])
        b = pool.submit(second.disable_pipeline_publisher, p['id'], p['destinations'][0]['id'])
        assert a.result(timeout=15) is True
        assert b.result(timeout=15) is True
    stored = one(live_forms, 'SELECT config FROM pipelines WHERE pipeline_id=:pid', pid=p['id'])['config']
    assert stored['inference_enabled'] is False
    assert stored['destinations'][0]['enabled'] is False
    assert stored['frame_source'] == p['frame_source']


@pytest.mark.parametrize('endpoint,ready', [('stream', True), ('stream/hq', True), ('stream', False)])
def test_management_readiness_head_has_no_viewer_effect(live_forms, endpoint, ready):
    import threading
    from types import SimpleNamespace
    from InferenceNode.pipeline import InferencePipeline
    p = management_pipeline(live_forms)
    manager = live_forms['node'].pipeline_manager
    instance = SimpleNamespace(_viewer_count=0, _is_streaming=False, _frame_lock=threading.Lock(),
        is_running=lambda: True, is_initialized=lambda: True,
        get_latest_frame=lambda: object() if ready else None)
    instance.start_streaming = lambda: InferencePipeline.start_streaming(instance)
    instance.stop_streaming = lambda: InferencePipeline.stop_streaming(instance)
    manager.active_pipelines[p['id']] = {'pipeline_instance': instance}
    try:
        response = live_forms['client'].head('/api/pipeline/'+p['id']+'/'+endpoint)
        assert response.status_code == (200 if ready else 503)
        response.close()
        assert instance._viewer_count == 0
        assert instance._is_streaming is False
    finally:
        manager.active_pipelines.pop(p['id'], None)


def test_management_runtime_failure_reports_saved_state(live_forms):
    from types import SimpleNamespace
    p = management_pipeline(live_forms)
    manager = live_forms['node'].pipeline_manager
    def fail():
        raise RuntimeError('isolated runtime failure')
    manager.active_pipelines[p['id']] = {'pipeline_instance': SimpleNamespace(disable_inference=fail)}
    try:
        response = live_forms['send']('/api/pipeline/'+p['id']+'/inference/disable')
        assert response.status_code == 503
        assert response.json['saved'] is True
        assert response.json['runtime_applied'] is False
        assert one(live_forms, 'SELECT config FROM pipelines WHERE pipeline_id=:pid', pid=p['id'])['config']['inference_enabled'] is False
    finally:
        manager.active_pipelines.pop(p['id'], None)


def test_management_db_failure_does_not_report_success_or_apply_runtime(live_forms, monkeypatch):
    from types import SimpleNamespace
    p = management_pipeline(live_forms)
    manager = live_forms['node'].pipeline_manager
    applied = []
    manager.active_pipelines[p['id']] = {'pipeline_instance': SimpleNamespace(disable_inference=lambda: applied.append(True))}
    def fail(*args):
        raise RuntimeError('isolated database failure')
    monkeypatch.setattr(manager.store, 'set_control', fail)
    try:
        response = live_forms['send']('/api/pipeline/'+p['id']+'/inference/disable')
        assert response.status_code == 500
        assert not response.json.get('saved')
        assert not applied
        assert one(live_forms, 'SELECT config FROM pipelines WHERE pipeline_id=:pid', pid=p['id'])['config']['inference_enabled'] is True
    finally:
        manager.active_pipelines.pop(p['id'], None)


# Models page ingestion regressions. Fixtures are structural, not inference-ready weights.
def models_upload(env, filename, content, engine='ultralytics', name='Audit'):
    return env['client'].post('/api/models/upload', headers={'X-CSRFToken':env['csrf']}, data={
        'file': (io.BytesIO(content), filename), 'engine_type':engine, 'name':name, 'description':'Audit description'})


@pytest.mark.parametrize('filename,content,engine', [
    ('empty.pt',b'','ultralytics'), ('invalid.pt',b'not a model','ultralytics'),
    ('invalid.pth',b'not a model','ultralytics'), ('unknown.pt',b'not a model','unknown_engine'),
    ('wrong.pt',b'not a model','onnx'),
])
def test_models_reject_invalid_upload_without_registry_row(live_forms, filename, content, engine):
    before = one(live_forms, 'SELECT count(*) AS n FROM models')['n']
    response = models_upload(live_forms, filename, content, engine)
    assert response.status_code == 400, response.json
    assert one(live_forms, 'SELECT count(*) AS n FROM models')['n'] == before


def test_models_geti_zip_is_stored_and_unsafe_zip_rejected(live_forms):
    import zipfile
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('model/model.xml', '<net/>')
        archive.writestr('model/model.bin', b'fixture')
    result = models_upload(live_forms, uuid.uuid4().hex+'.zip', stream.getvalue(), 'geti')
    assert result.status_code == 200, result.json
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, 'w') as archive:
        archive.writestr('../model.xml', '<net/>')
        archive.writestr('model.bin', b'fixture')
    assert models_upload(live_forms, 'unsafe.zip', bad.getvalue(), 'geti').status_code == 400


def test_models_repeat_preserves_metadata_and_engine_conflicts(live_forms, tmp_path):
    from InferenceNode.model_uploads import ModelConflictError
    filename = uuid.uuid4().hex+'.pt'
    data = model_checkpoint_bytes(filename)
    first = models_upload(live_forms, filename, data, name='First')
    second = models_upload(live_forms, filename, data, name='Second')
    assert first.status_code == second.status_code == 200
    assert first.json['model_id'] == second.json['model_id']
    assert one(live_forms, 'SELECT name FROM models WHERE model_id=:mid', mid=first.json['model_id'])['name'] == 'First'
    source = tmp_path/'model.pt'; source.write_bytes(data)
    with pytest.raises(ModelConflictError):
        live_forms['node'].model_repo.store_model(str(source), filename, 'onnx')


def test_models_concurrent_identical_uploads_are_idempotent(live_forms, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    source = tmp_path/'upload.pt'; source.write_bytes(model_checkpoint_bytes(uuid.uuid4().hex))
    filename = uuid.uuid4().hex+'.pt'
    barrier = threading.Barrier(2)
    def store():
        barrier.wait(timeout=10)
        return live_forms['node'].model_repo.store_model(str(source), filename, 'ultralytics')
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(store); b = pool.submit(store)
        first, second = a.result(timeout=20), b.result(timeout=20)
    assert first == second
    assert one(live_forms, 'SELECT count(*) AS n FROM models WHERE model_id=:mid', mid=first)['n'] == 1
    assert Path(live_forms['node'].model_repo.get_model_path(first)).read_bytes() == source.read_bytes()


def test_models_download_records_uploader_and_cleans_temporary_paths(live_forms, monkeypatch):
    from ultralytics.utils import downloads
    paths = []
    def download(path):
        paths.append(Path(path)); Path(path).write_bytes(model_checkpoint_bytes('download fixture'))
        return path
    monkeypatch.setattr(downloads, 'attempt_download_asset', download)
    for _ in range(2):
        response = live_forms['send']('/api/models/download-ultralytics', {'model_name':'yolo11n.pt', 'name':'Downloaded', 'description':'Source metadata'})
        assert response.status_code == 200, response.json
        row = one(live_forms, 'SELECT uploader_id,uploader_username,description FROM models WHERE model_id=:mid', mid=response.json['model_id'])
        assert row['uploader_id'] is not None and row['uploader_username']=='form_audit_admin'
        assert row['description']=='Source metadata'
    assert paths[0].parent != paths[1].parent
    assert all(not path.exists() for path in paths)
    rejected = live_forms['send']('/api/models/download-ultralytics', {'model_name':'/tmp/unapproved.pt'})
    assert rejected.status_code==400 and len(paths)==2


def test_models_special_character_ids_are_deletable(live_forms):
    from urllib.parse import quote
    response = models_upload(live_forms, "operator's #model-"+uuid.uuid4().hex+'.pt', model_checkpoint_bytes('special'))
    assert response.status_code == 200
    assert live_forms['send']('/api/models/'+quote(response.json['model_id'], safe=''), method='DELETE').status_code==200

# Publisher audit regressions: real routes and PostgreSQL persistence.
def publisher_fixture(env, **overrides):
    body = {'name': uuid.uuid4().hex, 'type': 'mqtt', 'config': {
        'server': 'broker.invalid', 'port': 1883, 'topic': 'faces', 'password': 'isolated-secret'}}
    body.update(overrides)
    r = env['send']('/api/publisher/favorites', body)
    assert r.status_code == 200, r.json
    return r.json['favorite']['id']


def test_publisher_missing_key_edit_preserves_ciphertext(live_forms):
    from InferenceNode import config_secrets as cs
    pid = publisher_fixture(live_forms)
    before = one(live_forms, 'SELECT config FROM publishers WHERE publisher_id=:id', id=pid)['config']
    cs.reload_keys('/nonexistent')
    try:
        r = live_forms['send']('/api/publisher/favorites/'+pid, {'config': {'password': '***'}}, method='PUT')
        assert r.status_code == 503
        assert one(live_forms, 'SELECT config FROM publishers WHERE publisher_id=:id', id=pid)['config'] == before
    finally:
        assert cs.reload_keys()


@pytest.mark.parametrize('typ,cfg', [('no-plugin', {}), ('mqtt', {}), ('mqtt', {'server':'b','topic':'x','port':-1}), ('null', {'rate_limit':-1}), ('webhook', {'headers':'bad header'})])
def test_publisher_invalid_form_is_400_without_row(live_forms, typ, cfg):
    name = uuid.uuid4().hex
    r = live_forms['send']('/api/publisher/favorites', {'name':name,'type':typ,'config':cfg})
    assert r.status_code == 400, r.json
    assert one(live_forms, 'SELECT count(*) n FROM publishers WHERE name=:name', name=name)['n'] == 0


def test_publisher_partial_update_and_type_switch(live_forms):
    from InferenceNode import publisher_store as pst
    pid = publisher_fixture(live_forms)
    assert live_forms['send']('/api/publisher/favorites/'+pid, {'config':{'password':'replacement'}}, method='PUT').status_code == 200
    config = pst.get_publisher(pid,runtime=True)['config']
    assert config['server']=='broker.invalid' and config['password']=='replacement'
    assert live_forms['send']('/api/publisher/favorites/'+pid, {'type':'null','config':{}}, method='PUT').status_code == 200
    assert pst.get_publisher(pid,runtime=True)['config']=={}


def test_publisher_concurrent_names_are_unique(live_forms):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier=Barrier(2); name=uuid.uuid4().hex
    cookie=live_forms['client'].get_cookie('session').value
    def create(_):
        with live_forms['node'].app.test_client() as client:
            client.set_cookie('session',cookie);barrier.wait(timeout=10)
            return client.post('/api/publisher/favorites',json={'name':name,'type':'null','config':{}},headers={'X-CSRFToken':live_forms['csrf']}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create,range(2)))==[200,409]
    assert one(live_forms,'SELECT count(*) n FROM publishers WHERE name=:name',name=name)['n']==1


def test_publisher_serialized_edits_preserve_new_password(live_forms,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from InferenceNode import publisher_store as pst, config_secrets as cs
    pid=publisher_fixture(live_forms);real=cs.decrypt_config;entered=Event();release=Event()
    def hold(config):
        result=real(config)
        if isinstance(config,dict) and 'server' in config and not entered.is_set():
            entered.set();assert release.wait(10)
        return result
    monkeypatch.setattr(cs,'decrypt_config',hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(pst.update_publisher,pid,config={'password':'new-secret'})
        assert entered.wait(10)
        b=pool.submit(pst.update_publisher,pid,config={'server':'changed.invalid','password':'***'})
        release.set();a.result(timeout=15);b.result(timeout=15)
    config=pst.get_publisher(pid,runtime=True)['config']
    assert config['password']=='new-secret' and config['server']=='changed.invalid'


def test_publisher_test_reports_missing_ids(live_forms):
    pid=publisher_fixture(live_forms,type='null',config={});missing=str(uuid.uuid4())
    r=live_forms['send']('/api/publisher/test-favorites',{'favorite_ids':[pid,missing],'message':{'test':True}})
    assert r.status_code==502 and set(r.json['results'])=={pid,missing}
    assert r.json['results'][pid]['status']=='success'


def test_publisher_webhook_test_requires_and_supplies_pipeline(live_forms,monkeypatch):
    import InferenceNode.inference_node as module
    pid=publisher_fixture(live_forms,type='webhook',config={'url':'http://receiver.invalid'})
    data={'favorite_ids':[pid],'message':{'test':True}}
    assert live_forms['send']('/api/publisher/test-favorites',data).status_code==400
    pipeline_id, _ = pipeline(live_forms)
    # Use the real saved pipeline ID. Stub delivery only; route and database remain real.
    seen=[]
    class Destination:
        def set_context_variables(self,**kw):self.context=kw
        def configure(self,**kw):pass
        def publish_once(self,message):seen.append((self.context,message));return {'status':'success'}
        def stop_queue(self):pass
        def close(self):pass
    monkeypatch.setattr(module,'ResultDestination',lambda typ:Destination())
    data['pipeline_id']=pipeline_id
    r=live_forms['send']('/api/publisher/test-favorites',data)
    assert r.status_code==200,r.json
    assert seen[0][0]['pipeline_id']==pipeline_id==seen[0][1]['pipeline_id']


def playable_video_bytes():
    import cv2, numpy as np, tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'clip.mp4')
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'mp4v'), 5, (32, 32))
        assert writer.isOpened()
        writer.write(np.zeros((32, 32, 3), dtype=np.uint8)); writer.release()
        return Path(path).read_bytes()

# Admin, telemetry and media regression checks (isolated PostgreSQL).
def three_user(env):
    r=env['send']('/api/users',{'username':uuid.uuid4().hex,'password':'Isolated-Password-123','role':'user','must_change_password':False})
    assert r.status_code==201
    return r.json['user']['id']

@pytest.mark.parametrize('payload',[{}, {'active':'false'}, {'active':1}])
def test_three_admin_requires_explicit_boolean(live_forms,payload):
    uid=three_user(live_forms)
    assert live_forms['send'](f'/api/users/{uid}/active',payload,method='PUT').status_code==400
    assert one(live_forms,'SELECT is_active FROM users WHERE id=:id',id=uid)['is_active']

def test_three_admin_validation_and_permission_audit_failure(live_forms,monkeypatch):
    uid=three_user(live_forms);pid,_=pipeline(live_forms)
    assert live_forms['send'](f'/api/users/{uid}',{'email':'invalid'},method='PATCH').status_code==400
    assert live_forms['send']('/api/users',['invalid']).status_code==400
    assert live_forms['send'](f'/api/pipelines/{pid}/access/{uid}',{'can_edit':'false'},method='PUT').status_code==400
    import InferenceNode.pipeline_access_routes as routes
    monkeypatch.setattr(routes,'record_audit',lambda **kw:(_ for _ in ()).throw(RuntimeError('isolated audit failure')))
    r=live_forms['send'](f'/api/pipelines/{pid}/access/{uid}',{'can_view':True},method='PUT')
    assert r.status_code==200 and r.json['saved'] and r.json['audit_recorded'] is False

def test_three_last_admin_concurrent_demotions(live_forms):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from InferenceNode.auth import service as svc
    from InferenceNode.auth.models import User
    # Leave exactly two admins for this isolated check, restoring all afterward.
    svc.create_user(None,username=uuid.uuid4().hex,password='Isolated-Password-123',role='admin')
    with live_forms['engine'].connect() as c:
        admins=c.execute(text("SELECT id,permissions_version FROM users WHERE role='admin' AND is_active")).all()
    with live_forms['engine'].begin() as c:
        for row in admins[2:]:c.execute(text("UPDATE users SET role='user' WHERE id=:id"),{'id':row.id})
    barrier=Barrier(2)
    def demote(row):
        barrier.wait(timeout=10)
        try:svc.set_role(None,row.id,'user');return 'saved'
        except svc.UserOpError:return 'refused'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(demote,admins[:2]))==['refused','saved']
        assert one(live_forms,"SELECT count(*) n FROM users WHERE role='admin' AND is_active")['n']==1
    finally:
        with live_forms['engine'].begin() as c:
            for row in admins:c.execute(text("UPDATE users SET role='admin',permissions_version=:v WHERE id=:id"),{'id':row.id,'v':row.permissions_version})

def three_telemetry(**kw):
    return {'enabled':False,'publish_interval':17,'mqtt_server':'','mqtt_port':1883,'mqtt_topic':'audit',**kw}

def test_three_telemetry_secrets_and_failed_save_preserve_state(live_forms,monkeypatch):
    from InferenceNode import config_secrets as cs,node_settings_store as nss
    assert live_forms['send']('/api/telemetry/configure',three_telemetry(mqtt_password='isolated-secret')).status_code==200
    before=one(live_forms,"SELECT value FROM node_settings WHERE key='telemetry'")['value']
    cs.reload_keys('/nonexistent')
    try:
        assert live_forms['send']('/api/telemetry/configure',three_telemetry()).status_code==503
        assert one(live_forms,"SELECT value FROM node_settings WHERE key='telemetry'")['value']==before
    finally:assert cs.reload_keys()
    monkeypatch.setattr(nss,'set_setting',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('isolated write failure')))
    assert live_forms['send']('/api/telemetry/configure',three_telemetry(publish_interval=33)).status_code==500
    assert live_forms['node'].telemetry.update_interval==17

def test_three_telemetry_disable_never_connects_and_errors_are_visible(live_forms,monkeypatch):
    t=live_forms['node'].telemetry
    def forbidden(**kw):raise AssertionError('Disable must not connect')
    monkeypatch.setattr(t,'configure_mqtt',forbidden)
    r=live_forms['send']('/api/telemetry/configure',three_telemetry(mqtt_server='broker.invalid'))
    assert r.status_code==200 and not t.running
    monkeypatch.setattr(t,'get_system_info',lambda:{'error':'isolated collection failure'})
    r=live_forms['client'].get('/api/telemetry');assert r.status_code==503 and r.json['available'] is False

def test_three_telemetry_restart_is_single_worker():
    from InferenceNode.telemetry import NodeTelemetry
    from threading import Event
    t=NodeTelemetry('isolated');sampled=Event();t.get_system_info=lambda:(sampled.set() or {})
    t.update_interval=300;t.start_telemetry();assert sampled.wait(5);old=t.telemetry_thread
    t.stop_telemetry();assert not old.is_alive()
    t.start_telemetry()
    try:assert t.telemetry_thread is not old and not old.is_alive()
    finally:t.stop_telemetry()

def three_media(env):
    r=env['client'].post('/api/media/upload-video',headers={'X-CSRFToken':env['csrf']},data={'file':(io.BytesIO(playable_video_bytes()),'test-'+uuid.uuid4().hex+'.mp4')})
    assert r.status_code==200,r.json
    return r.json

def test_three_media_validation_missing_and_delete_recovery(live_forms,monkeypatch):
    from InferenceNode import media_registry as mr,artifact_paths as ap
    r=live_forms['client'].post('/api/media/upload-video',headers={'X-CSRFToken':live_forms['csrf']},data={'file':(io.BytesIO(b'not a video'),'bad.mp4')})
    assert r.status_code==400
    row=three_media(live_forms);path=ap.resolve('media',row['relative_source']);real=mr.os.replace
    def fail(src,dst):
        if src==path:raise OSError('isolated move failure')
        return real(src,dst)
    monkeypatch.setattr(mr.os,'replace',fail)
    assert live_forms['send']('/api/media/'+row['media_id'],method='DELETE').status_code==500
    assert mr.servable_path(row['relative_source'])==path
    Path(path).unlink()
    result=live_forms['client'].get('/api/media')
    entry=next(m for m in result.json['media'] if m['media_id']==row['media_id'])
    assert entry['status']=='MISSING'

def test_three_media_unique_uploads_and_network_references(live_forms):
    from concurrent.futures import ThreadPoolExecutor
    from werkzeug.datastructures import FileStorage
    from InferenceNode import media_registry as mr
    from InferenceNode.pipeline_repository import repository
    def upload(_):return mr.ingest_upload(FileStorage(stream=io.BytesIO(b'fixture')),original_filename='same.mp4',timestamp='same-time')
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(upload,range(2)))
    assert rows[0]['relative_path']!=rows[1]['relative_path']
    pid,_=pipeline(live_forms);cfg=repository.get(pid)['config'];cfg['frame_source']={'capture_type':'ip_camera','config':{'source':'rtsp://camera.invalid/'+rows[0]['relative_path']}}
    repository.update(pid,config=cfg)
    assert not any(p['pipeline_id']==pid for p in mr.referencing_pipelines(rows[0]['relative_path']))

def test_three_media_reference_save_cannot_race_deletion(live_forms,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from InferenceNode import media_registry as mr
    from InferenceNode.pipeline_repository import repository
    row=three_media(live_forms);pid,_=pipeline(live_forms);entered=Event();release=Event();real=mr.referencing_pipelines
    def hold(rel):
        refs=real(rel);entered.set();assert release.wait(10);return refs
    monkeypatch.setattr(mr,'referencing_pipelines',hold)
    cfg=repository.get(pid)['config'];cfg['frame_source']={'capture_type':'video_file','config':{'relative_source':row['relative_source']}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion=pool.submit(mr.delete_media,row['media_id']);assert entered.wait(10)
        save=pool.submit(repository.update,pid,config=cfg);release.set()
        assert deletion.result(timeout=15)['outcome']=='deleted'
        with pytest.raises(ValueError):save.result(timeout=15)
