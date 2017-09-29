"""empty message

Revision ID: 85cd4bb0a09b
Revises: 2edaa8105387
Create Date: 2017-09-28 10:26:55.115889

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '85cd4bb0a09b'
down_revision = '2edaa8105387'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('link',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['scan_job.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_link_source_url'), 'link', ['source_url'], unique=False)
    op.create_index(op.f('ix_link_url'), 'link', ['url'], unique=False)
    op.drop_table('apscheduler_jobs')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('apscheduler_jobs',
    sa.Column('id', sa.VARCHAR(length=191), autoincrement=False, nullable=False),
    sa.Column('next_run_time', postgresql.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('job_state', postgresql.BYTEA(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='apscheduler_jobs_pkey')
    )
    op.drop_index(op.f('ix_link_url'), table_name='link')
    op.drop_index(op.f('ix_link_source_url'), table_name='link')
    op.drop_table('link')
    # ### end Alembic commands ###