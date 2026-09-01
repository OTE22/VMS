"""artifact registry foundation

PostgreSQL becomes the authoritative registry for models, model artifacts, inference
engines, publishers, node/telemetry configuration, media and thumbnails; the bytes stay
on persistent storage under ARTIFACT_ROOT. This migration only creates the FOUNDATION:

  * extend `models` (lifecycle status / validation_status / reason, task, framework, ...)
  * model_representations, model_artifacts   (one physical file = one artifact row)
  * inference_engines (origin builtin|custom, real lifecycle status, enabled invariant)
  * publishers, node_settings, media_assets, pipeline_thumbnails
  * pipelines.model_id  NULLABLE, NO foreign key, NO consistency CHECK yet

The pipeline -> model FK cannot be applied here: at this point the `models` table is
still empty (the JSON registry has not been migrated), while pipelines already reference
model ids in config. The registry data migration runs next; only THEN does 0005 back-fill
pipelines.model_id, add the FK (ON DELETE RESTRICT) and validate the consistency CHECK.
The application stays runnable after 0004.

Every status/validation_status/origin/kind column carries a CHECK generated from the SAME
enums the ORM uses (InferenceNode/artifact_states.py), so no undefined lifecycle value can
be persisted by any writer.

Revision ID: 0004_artifact_registry
Revises: 0003_pipeline_access
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa

# The vocabulary is imported from the single registry so the migration and the ORM
# cannot drift. env.py puts the repo on sys.path.
from InferenceNode.artifact_states import (STATUS_VALUES, VALIDATION_VALUES,   # noqa: E402
                                           ORIGIN_VALUES, KIND_VALUES)

revision = "0004_artifact_registry"
down_revision = "0003_pipeline_access"
branch_labels = None
depends_on = None


def _in(col, values):
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


_SHA = "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'"


def _fingerprint_cols():
    return [
        sa.Column("verified_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("verified_mtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("verified_ctime_ns", sa.BigInteger(), nullable=True),
        sa.Column("verified_inode", sa.BigInteger(), nullable=True),
        sa.Column("verified_device", sa.BigInteger(), nullable=True),
    ]


def _lifecycle_cols(prefix):
    return [
        sa.Column("status", sa.String(16), nullable=False, server_default="STAGING"),
        sa.Column("validation_status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.CheckConstraint(_in("status", STATUS_VALUES), name=f"ck_{prefix}_status"),
        sa.CheckConstraint(_in("validation_status", VALIDATION_VALUES),
                           name=f"ck_{prefix}_validation_status"),
    ]


def upgrade() -> None:
    # ---- models: extend the existing table (reuse, no models_v2)
    with op.batch_alter_table("models") as b:
        b.add_column(sa.Column("description", sa.Text(), nullable=True))
        b.add_column(sa.Column("task", sa.String(64), nullable=True))
        b.add_column(sa.Column("framework", sa.String(64), nullable=True))
        b.add_column(sa.Column("version", sa.String(64), nullable=True))
        b.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="STAGING"))
        b.add_column(sa.Column("validation_status", sa.String(32), nullable=False,
                               server_default="PENDING"))
        b.add_column(sa.Column("reason", sa.String(64), nullable=True))
        b.add_column(sa.Column("updated_at", sa.DateTime(), nullable=False,
                               server_default=sa.func.now()))
        b.create_check_constraint("ck_models_status", _in("status", STATUS_VALUES))
        b.create_check_constraint("ck_models_validation_status",
                                  _in("validation_status", VALIDATION_VALUES))
    op.create_index("idx_models_status", "models", ["status"])

    # ---- model_representations
    op.create_table(
        "model_representations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="primary"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("precision", sa.String(16), nullable=True),
        sa.Column("device_family", sa.String(32), nullable=True),
        sa.Column("runtime", sa.String(32), nullable=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        *_lifecycle_cols("model_repr"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(_in("kind", KIND_VALUES), name="ck_model_repr_kind"),
        sa.CheckConstraint("manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_model_repr_manifest_sha256"),
    )
    op.create_index("idx_model_repr_model", "model_representations", ["model_id"])

    # ---- model_artifacts (one physical file = one row)
    op.create_table(
        "model_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("representation_id", sa.Integer(),
                  sa.ForeignKey("model_representations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        *_lifecycle_cols("model_artifacts"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        *_fingerprint_cols(),
        sa.CheckConstraint(_SHA, name="ck_model_artifacts_sha256"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_model_artifacts_size"),
    )
    op.create_index("uq_model_artifacts_path", "model_artifacts", ["relative_path"], unique=True)
    op.create_index("idx_model_artifacts_model", "model_artifacts", ["model_id"])
    op.create_index("idx_model_artifacts_repr", "model_artifacts", ["representation_id"])

    # ---- inference_engines
    op.create_table(
        "inference_engines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("engine_key", sa.String(128), nullable=False),
        sa.Column("class_name", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("engine_type", sa.String(64), nullable=True),
        sa.Column("version", sa.String(64), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False, server_default="custom"),
        *_lifecycle_cols("engines"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("relative_path", sa.String(1024), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("shipped_version", sa.String(64), nullable=True),
        sa.Column("shipped_sha256", sa.String(64), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        *_fingerprint_cols(),
        sa.CheckConstraint(_in("origin", ORIGIN_VALUES), name="ck_engines_origin"),
        sa.CheckConstraint(_SHA, name="ck_engines_sha256"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_engines_size"),
        # exact invariant: enabled => AVAILABLE + PASSED (both halves in the DB)
        sa.CheckConstraint("enabled = FALSE OR (status = 'AVAILABLE' AND validation_status = 'PASSED')",
                           name="ck_engines_enabled_requires_available_passed"),
        sa.CheckConstraint("origin <> 'custom' OR (relative_path IS NOT NULL AND sha256 IS NOT NULL)",
                           name="ck_engines_custom_requires_artifact"),
    )
    op.create_index("uq_inference_engines_key", "inference_engines", ["engine_key"], unique=True)
    op.create_index("uq_inference_engines_path", "inference_engines", ["relative_path"], unique=True)

    # ---- publishers
    op.create_table(
        "publishers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("publisher_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="favorite"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('favorite', 'node_destination')", name="ck_publishers_kind"),
    )
    op.create_index("uq_publishers_publisher_id", "publishers", ["publisher_id"], unique=True)
    op.create_index("idx_publishers_kind", "publishers", ["kind"])

    # ---- node_settings
    op.create_table(
        "node_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("key IN ('node_identity', 'telemetry', 'preferences')",
                           name="ck_node_settings_key"),
    )

    # ---- media_assets
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_id", sa.String(36), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("media_type", sa.String(32), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        *_lifecycle_cols("media"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        *_fingerprint_cols(),
        sa.CheckConstraint(_SHA, name="ck_media_sha256"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_media_size"),
    )
    op.create_index("uq_media_assets_media_id", "media_assets", ["media_id"], unique=True)
    op.create_index("uq_media_assets_path", "media_assets", ["relative_path"], unique=True)

    # ---- pipeline_thumbnails
    op.create_table(
        "pipeline_thumbnails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_id", sa.Integer(), sa.ForeignKey("pipelines.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        *_lifecycle_cols("thumbs"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        *_fingerprint_cols(),
        sa.CheckConstraint(_SHA, name="ck_thumbs_sha256"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_thumbs_size"),
    )
    op.create_index("uq_pipeline_thumbnails_path", "pipeline_thumbnails", ["relative_path"], unique=True)
    op.create_index("idx_pipeline_thumbnails_pipeline", "pipeline_thumbnails", ["pipeline_id"])

    # ---- pipelines.model_id: nullable column ONLY (FK + CHECK come in 0005)
    op.add_column("pipelines", sa.Column("model_id", sa.String(255), nullable=True))
    op.create_index("idx_pipelines_model_id", "pipelines", ["model_id"])


def downgrade() -> None:
    op.drop_index("idx_pipelines_model_id", table_name="pipelines")
    op.drop_column("pipelines", "model_id")
    for name, indexes in (
        ("pipeline_thumbnails", ["idx_pipeline_thumbnails_pipeline", "uq_pipeline_thumbnails_path"]),
        ("media_assets", ["uq_media_assets_path", "uq_media_assets_media_id"]),
        ("node_settings", []),
        ("publishers", ["idx_publishers_kind", "uq_publishers_publisher_id"]),
        ("inference_engines", ["uq_inference_engines_path", "uq_inference_engines_key"]),
        ("model_artifacts", ["idx_model_artifacts_repr", "idx_model_artifacts_model", "uq_model_artifacts_path"]),
        ("model_representations", ["idx_model_repr_model"]),
    ):
        for ix in indexes:
            op.drop_index(ix, table_name=name)
        op.drop_table(name)
    op.drop_index("idx_models_status", table_name="models")
    with op.batch_alter_table("models") as b:
        b.drop_constraint("ck_models_validation_status", type_="check")
        b.drop_constraint("ck_models_status", type_="check")
        for col in ("updated_at", "reason", "validation_status", "status", "version",
                    "framework", "task", "description"):
            b.drop_column(col)
