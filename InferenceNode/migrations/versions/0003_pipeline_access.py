"""pipeline_user_access + app_state + pipelines.description

Makes PostgreSQL able to be the single source of truth for pipelines:
  - pipelines.description  : the last JSON-only persistent field
  - pipeline_user_access   : per-user authorization (owner_id stops being an authz input)
  - app_state              : application (NOT schema) migration state, so the JSON->DB
                             data migration has a completion flag that is independent of
                             alembic_version, table emptiness and backup-file existence.

Revision ID: 0003_pipeline_access
Revises: 0002_pipelines_models
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_pipeline_access"
down_revision = "0002_pipelines_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("description", sa.Text(), nullable=True))

    op.create_table(
        "pipeline_user_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        # CASCADE on both sides: deleting a pipeline or a user must never leave an
        # orphan grant that could later be re-attached to a recycled id.
        sa.Column("pipeline_id", sa.Integer(),
                  sa.ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("can_view", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_start", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_stop", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_edit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("pipeline_id", "user_id", name="uq_pipeline_user_access"),
    )
    op.create_index("idx_pua_pipeline", "pipeline_user_access", ["pipeline_id"])
    op.create_index("idx_pua_user", "pipeline_user_access", ["user_id"])
    op.create_index("idx_pua_created_by", "pipeline_user_access", ["created_by_id"])

    op.create_table(
        "app_state",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("app_state")
    op.drop_index("idx_pua_created_by", table_name="pipeline_user_access")
    op.drop_index("idx_pua_user", table_name="pipeline_user_access")
    op.drop_index("idx_pua_pipeline", table_name="pipeline_user_access")
    op.drop_table("pipeline_user_access")
    op.drop_column("pipelines", "description")
