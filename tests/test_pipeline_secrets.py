import json

import pytest
from sqlalchemy import text
from InferenceNode import config_secrets as cs, pipeline_secrets as ps
from InferenceNode.auth import db
from InferenceNode.auth.models import Base
from InferenceNode.pipeline_repository import repository


@pytest.fixture
def store(tmp_path):
    import InferenceNode.data_models
    db._engine = None; db._SessionLocal = None
    engine = db.init_engine(f'sqlite:///{tmp_path / "pipelines.db"}')
    Base.metadata.create_all(engine)
    key = tmp_path / 'key'
    key.write_text(cs.generate_key_line('pipeline-test')); key.chmod(0o600)
    cs.reload_keys(str(key))
    yield engine, key
    engine.dispose()
    db._engine = None; db._SessionLocal = None
    cs.reload_keys('/nonexistent')


def raw(engine):
    with engine.connect() as conn:
        return json.loads(conn.execute(text("SELECT config FROM pipelines WHERE pipeline_id='p'")).scalar_one())


def payload():
    return {'model':{'id':None,'engine_type':'pass'},
            'frame_source':{'config':{'source':'rtsp://alice:camera-secret@camera/live','password':' camera-secret '}},
            'destinations':[{'type':'mqtt','config':{'password':'broker-secret','port':1883}}],
            'tokens':['nested-secret'], 'inference_enabled':False}


def test_database_ciphertext_and_exact_runtime_roundtrip(store):
    engine, key = store
    repository.create(pipeline_id='p',config=payload())
    saved = raw(engine)
    for secret in ['camera-secret','broker-secret','nested-secret','alice']:
        assert secret not in json.dumps(saved)
    assert saved['model']['id'] is None
    assert repository.get('p')['config'] == payload()
    assert repository.list(is_admin=True)[0]['config'] == payload()
    repository.update('p',config=payload())
    assert repository.get('p')['config'] == payload()


def test_missing_key_fails_closed_without_destroying_stored_config(store):
    engine, key = store
    repository.create(pipeline_id='p',config=payload())
    before = raw(engine)
    cs.reload_keys('/nonexistent')
    with pytest.raises(cs.SecretsUnavailable): repository.get('p')
    with pytest.raises(cs.SecretsUnavailable): repository.update('p',config=payload())
    assert raw(engine) == before
    cs.reload_keys(str(key))
    assert repository.get('p')['config'] == payload()


def test_legacy_migration_atomic_idempotent_and_key_required(store):
    engine, key = store
    repository.create(pipeline_id='p',config={})
    with engine.begin() as conn:
        conn.execute(text('UPDATE pipelines SET config=:config'),{'config':json.dumps(payload())})
    cs.reload_keys('/nonexistent')
    with pytest.raises(cs.SecretsUnavailable): ps.migrate_existing()
    assert raw(engine) == payload()
    cs.reload_keys(str(key))
    assert ps.migrate_existing() == 1
    encrypted = raw(engine)
    assert ps.migrate_existing() == 0
    assert raw(engine) == encrypted
    assert repository.get('p')['config'] == payload()


def test_rotation_reads_old_key_and_reencrypts_on_save(store):
    engine, key = store
    repository.create(pipeline_id='p',config=payload())
    old_line = key.read_text()
    key.write_text(cs.generate_key_line('rotated')+'\n'+old_line)
    cs.reload_keys(str(key))
    repository.update('p',config=repository.get('p')['config'])
    assert raw(engine)['frame_source']['config']['password']['key_id'] == 'rotated'
    key.write_text(key.read_text().splitlines()[0])
    cs.reload_keys(str(key))
    assert repository.get('p')['config'] == payload()


@pytest.fixture
def encryption_pg():
    # Dedicated database: this migration deliberately scans every pipeline row.
    from pg_fixture import isolated_pg
    yield from isolated_pg.__wrapped__()


def test_existing_postgresql_row_migrates_without_changing_model_reference(encryption_pg, tmp_path):
    from sqlalchemy import select
    from InferenceNode.data_models import Pipeline
    key = tmp_path / 'pg-key'
    key.write_text(cs.generate_key_line('pg-pipeline')); key.chmod(0o600)
    cs.reload_keys(str(key))
    db._engine = None; db._SessionLocal = None
    db.init_engine(encryption_pg['url'])
    try:
        with db.get_session() as session:
            session.add(Pipeline(pipeline_id='encryption-migration-pg',config=payload(),status='stopped'))
        assert ps.migrate_existing() >= 1
        assert repository.get('encryption-migration-pg')['config'] == payload()
        with db.get_session() as session:
            row = session.execute(select(Pipeline).where(Pipeline.pipeline_id=='encryption-migration-pg')).scalar_one()
            assert row.model_id is None and row.config['model']['id'] is None
            assert 'camera-secret' not in json.dumps(row.config)
            assert 'broker-secret' not in json.dumps(row.config)
        assert ps.migrate_existing() == 0
    finally:
        db._engine = None; db._SessionLocal = None
        cs.reload_keys('/nonexistent')
