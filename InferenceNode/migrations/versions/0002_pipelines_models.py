"""pipelines + models (ArmyEye config with per-user ownership)

Revision ID: 0002_pipelines_models
Revises: 0001_users_audit
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_pipelines_models"
down_revision = "0001_users_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipelines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("owner_username", sa.String(length=100), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="stopped"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_pipelines_pipeline_id", "pipelines", ["pipeline_id"], unique=True)
    op.create_index("idx_pipelines_owner", "pipelines", ["owner_id"])
    op.create_index("idx_pipelines_status", "pipelines", ["status"])

    op.create_table(
        "models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("uploader_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("uploader_username", sa.String(length=100), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("engine_type", sa.String(length=100), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_models_model_id", "models", ["model_id"], unique=True)
    op.create_index("idx_models_uploader", "models", ["uploader_id"])
    op.create_index("idx_models_engine_type", "models", ["engine_type"])


def downgrade() -> None:
    op.drop_index("idx_models_engine_type", table_name="models")
    op.drop_index("idx_models_uploader", table_name="models")
    op.drop_index("uq_models_model_id", table_name="models")
    op.drop_table("models")
    op.drop_index("idx_pipelines_status", table_name="pipelines")
    op.drop_index("idx_pipelines_owner", table_name="pipelines")
    op.drop_index("uq_pipelines_pipeline_id", table_name="pipelines")
    op.drop_table("pipelines")
