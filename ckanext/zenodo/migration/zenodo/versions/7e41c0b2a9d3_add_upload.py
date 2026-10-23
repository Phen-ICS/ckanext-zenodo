"""Add fair_zenodo_upload.

Files being uploaded in chunks, until they're on Zenodo (see
lib/chunked_upload.py).

Revision ID: 7e41c0b2a9d3
Revises: 5c7e9a2b4d10
Create Date: 2026-10-23 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = '7e41c0b2a9d3'
down_revision = '5c7e9a2b4d10'
branch_labels = None
depends_on = None


def upgrade():
    insp = Inspector.from_engine(op.get_bind())
    if 'fair_zenodo_upload' in insp.get_table_names():
        return
    op.create_table(
        'fair_zenodo_upload',
        sa.Column('id', sa.UnicodeText, primary_key=True),
        sa.Column(
            'package_id',
            sa.UnicodeText,
            sa.ForeignKey('package.id', onupdate='CASCADE', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('user_id', sa.UnicodeText, nullable=False),
        sa.Column('filename', sa.UnicodeText, nullable=False),
        sa.Column('size', sa.BigInteger, nullable=False),
        sa.Column('received', sa.BigInteger, nullable=False, server_default='0'),
        sa.Column('forced', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('status', sa.UnicodeText, nullable=False),
        sa.Column('error', sa.UnicodeText, nullable=True),
        sa.Column('send_attempts', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created', sa.DateTime, nullable=False),
        sa.Column('updated', sa.DateTime, nullable=False),
    )
    op.create_index(
        'idx_fair_zenodo_upload_package_id', 'fair_zenodo_upload', ['package_id']
    )


def downgrade():
    op.drop_table('fair_zenodo_upload')
