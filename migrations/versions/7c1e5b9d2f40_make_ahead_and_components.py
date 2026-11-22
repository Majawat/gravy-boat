"""make-ahead dishes, sub-recipe components, whole batches, turkey thaw weight

Revision ID: 7c1e5b9d2f40
Revises: d02e2cc040ce
Create Date: 2026-11-22 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

import app.models  # custom column types (JSONText)


# revision identifiers, used by Alembic.
revision = '7c1e5b9d2f40'
down_revision = 'd02e2cc040ce'
branch_labels = None
depends_on = None

# Plain ALTER TABLE ADD COLUMN, never batch mode: batch mode rebuilds the table by dropping it, and with
# foreign keys on, dropping `dish` cascades and deletes every step (ON DELETE CASCADE).
DISH_COLUMNS = [
    sa.Column('make_ahead_days', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('make_ahead_time', sa.Time(), nullable=True),
    sa.Column('whole_batches', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('yield_qty', sa.Float(), nullable=True),
    sa.Column('yield_unit', sa.String(length=60), nullable=True),
    sa.Column('components', app.models.JSONText(), nullable=False, server_default='[]'),
    sa.Column('thaw_lbs', sa.Float(), nullable=True),
]


def upgrade():
    for col in DISH_COLUMNS:
        op.add_column('dish', col)
    op.add_column('step', sa.Column('uses', app.models.JSONText(), nullable=False, server_default='[]'))


def downgrade():
    # Dropping columns on SQLite rebuilds the table, so keep the cascade from wiping child rows.
    op.execute('PRAGMA foreign_keys=OFF')
    with op.batch_alter_table('step', schema=None) as batch_op:
        batch_op.drop_column('uses')
    with op.batch_alter_table('dish', schema=None) as batch_op:
        for col in reversed(DISH_COLUMNS):
            batch_op.drop_column(col.name)
    op.execute('PRAGMA foreign_keys=ON')
