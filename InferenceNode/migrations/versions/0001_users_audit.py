"""users + audit_log (ArmyEye self-owned auth)

Revision ID: 0001_users_audit
Revises:
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_users_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("username_key", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("permissions_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role in ('admin','user')", name="ck_users_role"),
    )
    op.create_index("uq_users_username_key", "users", ["username_key"], unique=True)
    op.create_index("idx_users_role", "users", ["role"])
    op.create_index("idx_users_active", "users", ["is_active"])
    op.create_index("idx_users_created", "users", ["created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_username", sa.String(length=100), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_audit_action_created", "audit_log", ["action", "created_at"])
    op.create_index("idx_audit_created", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_created", table_name="audit_log")
    op.drop_index("idx_audit_action_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("idx_users_created", table_name="users")
    op.drop_index("idx_users_active", table_name="users")
    op.drop_index("idx_users_role", table_name="users")
    op.drop_index("uq_users_username_key", table_name="users")
    op.drop_table("users")
