"""Enforce artifact parent agreement and protect JSON model references.

Preserves all columns, payloads and rows. Existing inconsistent rows cause an
actionable failure and transactional rollback rather than guessed repairs.

Revision ID: 0007_reference_integrity
Revises: 0006_pipeline_node_assignment
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_reference_integrity"
down_revision = "0006_pipeline_node_assignment"
branch_labels = None
depends_on = None

_OLD_CHECK = (
    "model_id IS NULL OR (config->'model'->>'id') IS NULL "
    "OR config->'model'->>'id' = model_id"
)
_NEW_CHECK = (
    "(config->'model'->>'id') IS NULL OR "
    "(model_id IS NOT NULL AND config->'model'->>'id' = model_id)"
)


def upgrade():
    conn = op.get_bind()
    # Avoid waiting indefinitely behind busy application transactions. PostgreSQL
    # transactional DDL leaves the old schema intact if any step fails.
    conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    bad_artifacts = conn.execute(sa.text(
        "SELECT count(*) FROM model_artifacts a "
        "JOIN model_representations r ON r.id = a.representation_id "
        "WHERE a.model_id <> r.model_id"
    )).scalar_one()
    bad_pipelines = conn.execute(sa.text(
        f"SELECT count(*) FROM pipelines WHERE NOT ({_NEW_CHECK})"
    )).scalar_one()
    if bad_artifacts or bad_pipelines:
        raise RuntimeError(
            "0007 reference integrity preflight failed: "
            f"{bad_artifacts} artifact parent mismatches, "
            f"{bad_pipelines} unprotected/conflicting pipeline model references. "
            "Reconcile these references explicitly before retrying; no rows were changed."
        )

    op.create_unique_constraint("uq_model_repr_id_model", "model_representations",
                                ["id", "model_id"])
    op.create_foreign_key(
        "fk_artifact_representation_model", "model_artifacts", "model_representations",
        ["representation_id", "model_id"], ["id", "model_id"], ondelete="CASCADE",
    )
    op.drop_constraint("ck_pipelines_model_ref_consistent", "pipelines", type_="check")
    op.create_check_constraint("ck_pipelines_model_ref_consistent", "pipelines", _NEW_CHECK)


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_pipelines_model_ref_consistent", "pipelines", type_="check")
    op.create_check_constraint("ck_pipelines_model_ref_consistent", "pipelines", _OLD_CHECK)
    op.drop_constraint("fk_artifact_representation_model", "model_artifacts", type_="foreignkey")
    op.drop_constraint("uq_model_repr_id_model", "model_representations", type_="unique")
