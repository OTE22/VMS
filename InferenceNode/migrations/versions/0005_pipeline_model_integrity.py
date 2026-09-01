"""pipeline -> model relational integrity

Runs ONLY after the model registry has been populated (registry data migration, Phase 6).
0004 added `pipelines.model_id` as a nullable column with no FK because `models` was
still empty while pipelines already referenced model ids inside `config`.

This migration:
  1. audits every pipeline's config->'model'->>'id' and classifies it
       VALID | UNKNOWN_MODEL | MISSING_MODEL_ID | MALFORMED_CONFIG
     (counts are logged; UNKNOWN references are REPORTED, never mapped by guessing)
  2. back-fills pipelines.model_id for VALID rows only
  3. adds fk_pipelines_model -> models(model_id) ON DELETE RESTRICT
     (a referenced model can no longer be deleted - enforced by the DATABASE, closing
      the "check refs, assign, delete" race)
  4. adds ck_pipelines_model_ref_consistent  NOT VALID, then VALIDATEs it when the audit
     shows zero violators - a JSON->>'id' on the `json` type is IMMUTABLE and legal in a
     CHECK; the two IS NULL arms cover missing model object / null id / malformed config.
     If validation is impossible on a deployment the constraint stays NOT VALID (still
     enforced for every NEW write) and startup verification reports the legacy violators.

Canonical model identity is pipelines.model_id; config.model.id is a serialized
reflection kept in sync by PipelineRepository only.

Revision ID: 0005_pipeline_model_integrity
Revises: 0004_artifact_registry
Create Date: 2026-08-19
"""
import json
import logging

from alembic import op
import sqlalchemy as sa

revision = "0005_pipeline_model_integrity"
down_revision = "0004_artifact_registry"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.0005")


def _audit(conn):
    """Classify every pipeline's model reference. Returns (counts, valid_pairs)."""
    known = {r[0] for r in conn.execute(sa.text("SELECT model_id FROM models"))}
    counts = {"VALID": 0, "UNKNOWN_MODEL": 0, "MISSING_MODEL_ID": 0, "MALFORMED_CONFIG": 0}
    valid = []
    unknown = []
    for pid, cfg in conn.execute(sa.text("SELECT pipeline_id, config FROM pipelines")):
        try:
            data = cfg if isinstance(cfg, dict) else json.loads(cfg) if cfg else {}
        except Exception:
            counts["MALFORMED_CONFIG"] += 1
            continue
        model = data.get("model") if isinstance(data, dict) else None
        if not isinstance(model, dict):
            counts["MALFORMED_CONFIG"] += 1
            continue
        mid = model.get("id")
        if mid in (None, ""):
            counts["MISSING_MODEL_ID"] += 1
            continue
        if str(mid) in known:
            counts["VALID"] += 1
            valid.append((pid, str(mid)))
        else:
            counts["UNKNOWN_MODEL"] += 1
            unknown.append((pid, str(mid)))
    return counts, valid, unknown


def upgrade() -> None:
    conn = op.get_bind()
    counts, valid, unknown = _audit(conn)
    log.warning(f"[0005] pipeline->model reference audit: {counts}")
    for pid, mid in unknown:
        log.warning(f"[0005] UNKNOWN_MODEL: pipeline {pid} references model {mid!r} "
                    f"(no registry row) - left NULL, reported, NOT guessed")

    # 2. back-fill VALID rows only
    for pid, mid in valid:
        conn.execute(sa.text("UPDATE pipelines SET model_id = :m WHERE pipeline_id = :p"),
                     {"m": mid, "p": pid})

    # 3. FK - RESTRICT: the database refuses to delete a referenced model
    op.create_foreign_key("fk_pipelines_model", "pipelines", "models",
                          ["model_id"], ["model_id"], ondelete="RESTRICT")

    # 4. consistency CHECK - staged rollout (NOT VALID -> VALIDATE)
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text(
            "ALTER TABLE pipelines ADD CONSTRAINT ck_pipelines_model_ref_consistent CHECK ("
            "  model_id IS NULL"
            "  OR (config->'model'->>'id') IS NULL"
            "  OR config->'model'->>'id' = model_id"
            ") NOT VALID"))
        violators = conn.execute(sa.text(
            "SELECT count(*) FROM pipelines WHERE NOT ("
            "  model_id IS NULL OR (config->'model'->>'id') IS NULL "
            "  OR config->'model'->>'id' = model_id)")).scalar_one()
        if violators == 0:
            conn.execute(sa.text("ALTER TABLE pipelines VALIDATE CONSTRAINT ck_pipelines_model_ref_consistent"))
            log.warning("[0005] ck_pipelines_model_ref_consistent VALIDATED (0 violators)")
        else:
            log.warning(f"[0005] ck_pipelines_model_ref_consistent left NOT VALID: {violators} legacy "
                        f"violators (still enforced for new writes; startup verification reports them)")
    else:
        # SQLite (unit tests): no JSON operators in CHECK; the repository invariant +
        # test_pipeline_model_column_matches_json_reference cover it there.
        pass


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("ALTER TABLE pipelines DROP CONSTRAINT IF EXISTS ck_pipelines_model_ref_consistent"))
    op.drop_constraint("fk_pipelines_model", "pipelines", type_="foreignkey")
    # column + index stay (they belong to 0004)
