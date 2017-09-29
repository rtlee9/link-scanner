"""empty message

Revision ID: 371b3d91e616
Revises: 31a9133ed7e7
Create Date: 2017-09-27 00:26:19.105539

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '371b3d91e616'
down_revision = '31a9133ed7e7'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('link_check', sa.Column('text', sa.Text(), nullable=True))
    op.add_column('link_check', sa.Column('url_raw', sa.Text(), nullable=True))
    op.drop_column('link_check', 'headers')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('link_check', sa.Column('headers', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True))
    op.drop_column('link_check', 'url_raw')
    op.drop_column('link_check', 'text')
    # ### end Alembic commands ###