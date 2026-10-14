"""Initialisation.

Revision ID: bc8cf8f1144e
Revises:
Create Date: 2026-10-14 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = 'bc8cf8f1144e'
down_revision = None
branch_labels = None
depends_on = None


def _table_exists(name):
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    return name in insp.get_table_names()


def upgrade():
    if not _table_exists('fair_zenodo_deposition'):
        op.create_table(
            'fair_zenodo_deposition',
            sa.Column(
                'package_id',
                sa.UnicodeText,
                sa.ForeignKey('package.id', onupdate='CASCADE', ondelete='CASCADE'),
                primary_key=True,
            ),
            sa.Column('zenodo_deposition_id', sa.UnicodeText, nullable=False),
            sa.Column('bucket_url', sa.UnicodeText, nullable=False),
            sa.Column('zenodo_record_id', sa.UnicodeText, nullable=True),
            sa.Column('zenodo_record_doi', sa.UnicodeText, nullable=True),
            sa.Column(
                'is_sandbox', sa.Boolean, nullable=False, server_default=sa.true()
            ),
            sa.Column(
                'published', sa.Boolean, nullable=False, server_default=sa.false()
            ),
            sa.Column('file_count', sa.Integer, nullable=False, server_default='0'),
            sa.Column('last_metadata_hash', sa.UnicodeText, nullable=True),
            sa.Column('created', sa.DateTime, nullable=False),
            sa.Column('updated', sa.DateTime, nullable=False),
        )


def downgrade():
    op.drop_table('fair_zenodo_deposition')
