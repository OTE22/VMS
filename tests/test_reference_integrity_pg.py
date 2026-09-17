"""Regression proofs on fixture-owned PostgreSQL, never the running database."""
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_registry_schema_pg import _alembic, HEAD


def _model(c, name):
    return c.execute(text("INSERT INTO models (model_id) VALUES (:n) RETURNING id"),
                     {"n": name}).scalar_one()


def _representation(c, model):
    return c.execute(text(
        "INSERT INTO model_representations (model_id, format) VALUES (:m, 'onnx') RETURNING id"
    ), {"m": model}).scalar_one()


def test_artifact_parent_agreement_and_cascade(pg_guard):
    with pg_guard["engine"].connect() as c:
        tx = c.begin()
        try:
            a, b = _model(c, "parent-a"), _model(c, "parent-b")
            rep = _representation(c, a)
            insert = text("INSERT INTO model_artifacts (model_id, representation_id, relative_path) "
                          "VALUES (:m, :r, :p)")
            with pytest.raises(IntegrityError):
                with c.begin_nested():
                    c.execute(insert, {"m": b, "r": rep, "p": "bad.onnx"})
            c.execute(insert, {"m": a, "r": rep, "p": "good.onnx"})
            # Moving either side independently must also be rejected.
            for sql, params in [
                ("UPDATE model_artifacts SET model_id=:m WHERE representation_id=:r", {"m": b, "r": rep}),
                ("UPDATE model_representations SET model_id=:m WHERE id=:r", {"m": b, "r": rep}),
            ]:
                with pytest.raises(IntegrityError):
                    with c.begin_nested():
                        c.execute(text(sql), params)
            c.execute(text("DELETE FROM models WHERE id=:m"), {"m": a})
            assert c.execute(text("SELECT count(*) FROM model_artifacts WHERE representation_id=:r"),
                             {"r": rep}).scalar_one() == 0
        finally:
            tx.rollback()


@pytest.mark.parametrize("column,json_id,accepted", [
    (None, "known", False), (None, "unknown", False),
    ("known", "other", False), ("unknown", "unknown", False),
    ("known", "known", True), (None, None, True), ("known", None, True),
])
def test_pipeline_reference_null_semantics(pg_guard, column, json_id, accepted):
    with pg_guard["engine"].connect() as c:
        tx = c.begin()
        try:
            _model(c, "known")
            query = text("INSERT INTO pipelines (pipeline_id, model_id, config) "
                         "VALUES ('reference-case', :m, CAST(:cfg AS json))")
            params = {"m": column, "cfg": json.dumps({"model": {"id": json_id}})}
            if accepted:
                c.execute(query, params)
            else:
                with pytest.raises(IntegrityError):
                    with c.begin_nested():
                        c.execute(query, params)
        finally:
            tx.rollback()


@pytest.mark.parametrize("bad_kind", ["artifact", "pipeline"])
def test_upgrade_refuses_legacy_conflicts_without_changing_rows(pg_guard, bad_kind):
    assert _alembic(pg_guard, "downgrade", "0006_pipeline_node_assignment").returncode == 0
    try:
        with pg_guard["engine"].begin() as c:
            a, b = _model(c, "legacy-a"), _model(c, "legacy-b")
            rep = _representation(c, a)
            if bad_kind == "artifact":
                c.execute(text("INSERT INTO model_artifacts (model_id, representation_id, relative_path) "
                               "VALUES (:m, :r, 'legacy-bad.onnx')"), {"m": b, "r": rep})
            else:
                c.execute(text("INSERT INTO pipelines (pipeline_id, config) "
                               "VALUES ('legacy-bad', '{\"model\":{\"id\":\"legacy-a\"}}')"))
        result = _alembic(pg_guard, "upgrade", "head")
        assert result.returncode != 0
        assert "reference integrity preflight failed" in result.stderr + result.stdout
        with pg_guard["engine"].connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006_pipeline_node_assignment"
            if bad_kind == "artifact":
                assert c.execute(text("SELECT model_id FROM model_artifacts WHERE relative_path='legacy-bad.onnx'")).scalar_one() == b
            else:
                assert c.execute(text("SELECT model_id FROM pipelines WHERE pipeline_id='legacy-bad'")).scalar_one() is None
    finally:
        with pg_guard["engine"].begin() as c:
            c.execute(text("DELETE FROM pipelines WHERE pipeline_id='legacy-bad'"))
            c.execute(text("DELETE FROM models WHERE model_id IN ('legacy-a', 'legacy-b')"))
        up = _alembic(pg_guard, "upgrade", "head")
        assert up.returncode == 0, up.stdout + up.stderr
    with pg_guard["engine"].connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def test_populated_upgrade_preserves_payload_and_artifact_ids(pg_guard):
    assert _alembic(pg_guard, "downgrade", "0006_pipeline_node_assignment").returncode == 0
    cfg = {"model": {"id": "preserved"}, "destinations": [{"id": "d1", "type": "mqtt"}]}
    try:
        with pg_guard["engine"].begin() as c:
            m = _model(c, "preserved")
            rep = _representation(c, m)
            artifact = c.execute(text(
                "INSERT INTO model_artifacts (model_id, representation_id, relative_path) "
                "VALUES (:m, :r, 'preserved.onnx') RETURNING id"
            ), {"m": m, "r": rep}).scalar_one()
            c.execute(text("INSERT INTO pipelines (pipeline_id, model_id, config) "
                           "VALUES ('preserved', 'preserved', CAST(:cfg AS json))"),
                      {"cfg": json.dumps(cfg)})
        up = _alembic(pg_guard, "upgrade", "head")
        assert up.returncode == 0, up.stdout + up.stderr
        with pg_guard["engine"].connect() as c:
            assert c.execute(text("SELECT config FROM pipelines WHERE pipeline_id='preserved'")).scalar_one() == cfg
            assert c.execute(text("SELECT id, model_id, representation_id FROM model_artifacts "
                                  "WHERE relative_path='preserved.onnx'")).one() == (artifact, m, rep)
    finally:
        with pg_guard["engine"].begin() as c:
            c.execute(text("DELETE FROM pipelines WHERE pipeline_id='preserved'"))
            c.execute(text("DELETE FROM models WHERE model_id='preserved'"))
        up = _alembic(pg_guard, "upgrade", "head")
        assert up.returncode == 0, up.stdout + up.stderr
