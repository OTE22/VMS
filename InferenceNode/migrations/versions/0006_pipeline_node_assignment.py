"""pipeline -> node assignment (horizontal workers)

A single ArmyEye process tops out around 220-250 inferences/second: at the collapse point
GPU sits near 25% and CPU near 5 of 20 cores, so the ceiling is the GIL, not hardware.
60 cameras at 5 fps needs 300 inferences/second and is therefore impossible in one process,
while THREE processes of 20 cameras deliver it comfortably (measured: 60/60 cameras at
5.00 fps, 9.99/20 cores, 50% GPU).

Nothing in the schema said WHICH node runs a pipeline, so two instances sharing this
database had no way to divide the work - both would answer for every pipeline and either
could start the same camera twice.

This migration adds `pipelines.node_id`:
    NULL      -> unassigned; any node may run it (the existing single-node behaviour,
                 which is why this is a pure additive change and needs no back-fill)
    '<id>'    -> only the node whose ARMYEYE_NODE_ID matches may run it

Deliberately NOT a foreign key: nodes are runtime processes, not rows. A node that is
retired must leave its pipeline definitions intact and reassignable, and a pipeline must
survive being pointed at a node that does not exist yet.

Revision ID: 0006_pipeline_node_assignment
Revises: 0005_pipeline_model_integrity
Create Date: 2026-09-08
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "0006_pipeline_node_assignment"
down_revision = "0005_pipeline_model_integrity"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.0006")


def _has_column(bind, table, column):
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    if _has_column(bind, "pipelines", "node_id"):
        log.info("pipelines.node_id already present; nothing to do")
        return

    op.add_column("pipelines", sa.Column("node_id", sa.String(length=255), nullable=True))
    # Assignment is queried on every start and on every node's pipeline listing.
    op.create_index("ix_pipelines_node_id", "pipelines", ["node_id"])

    total = bind.execute(sa.text("SELECT count(*) FROM pipelines")).scalar() or 0
    log.info("pipelines.node_id added; %d existing pipeline(s) left UNASSIGNED "
             "(runnable on any node, i.e. unchanged behaviour)", total)


def downgrade():
    bind = op.get_bind()
    if not _has_column(bind, "pipelines", "node_id"):
        return
    op.drop_index("ix_pipelines_node_id", table_name="pipelines")
    op.drop_column("pipelines", "node_id")
