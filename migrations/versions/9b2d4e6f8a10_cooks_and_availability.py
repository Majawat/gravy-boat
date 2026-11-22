"""named cooks, away times, dish cook, stretchable waits, quiet hours

Revision ID: 9b2d4e6f8a10
Revises: 7c1e5b9d2f40
Create Date: 2026-11-22 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9b2d4e6f8a10'
down_revision = '7c1e5b9d2f40'
branch_labels = None
depends_on = None

# New tables plus plain ALTER TABLE ADD COLUMN only. Never batch mode on `dish`: it rebuilds the table, and
# with foreign keys on, dropping `dish` cascade-deletes every step.


def upgrade():
    op.create_table(
        'cook',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('mealie_user_id', sa.String(length=64), nullable=True),
        sa.Column('sort', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_cook_household_id'), 'cook', ['household_id'], unique=False)
    op.create_table(
        'away_block',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('cook_id', sa.Integer(), nullable=True),
        sa.Column('start', sa.DateTime(), nullable=False),
        sa.Column('end', sa.DateTime(), nullable=False),
        sa.Column('label', sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(['cook_id'], ['cook.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['event_id'], ['event.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_away_block_event_id'), 'away_block', ['event_id'], unique=False)
    # Alembic only adds a column with a foreign key through batch mode on SQLite; SQLite itself can do it.
    op.execute('ALTER TABLE dish ADD COLUMN cook_id INTEGER REFERENCES cook (id) ON DELETE SET NULL')
    op.add_column('step', sa.Column('stretch', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('kitchen_settings', sa.Column('quiet_start', sa.Time(), nullable=True))
    op.add_column('kitchen_settings', sa.Column('quiet_end', sa.Time(), nullable=True))


def downgrade():
    op.execute('PRAGMA foreign_keys=OFF')
    with op.batch_alter_table('kitchen_settings', schema=None) as b:
        b.drop_column('quiet_end')
        b.drop_column('quiet_start')
    with op.batch_alter_table('step', schema=None) as b:
        b.drop_column('stretch')
    with op.batch_alter_table('dish', schema=None) as b:
        b.drop_column('cook_id')
    op.drop_index(op.f('ix_away_block_event_id'), table_name='away_block')
    op.drop_table('away_block')
    op.drop_index(op.f('ix_cook_household_id'), table_name='cook')
    op.drop_table('cook')
    op.execute('PRAGMA foreign_keys=ON')
