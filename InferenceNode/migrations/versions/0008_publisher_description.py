"""Persist publisher favorite descriptions without replacing existing rows."""
from alembic import op
import sqlalchemy as sa

revision = "0008_publisher_description"
down_revision = "0007_reference_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("publishers", sa.Column("description", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("publishers", "description")
