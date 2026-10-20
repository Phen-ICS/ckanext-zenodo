"""Add pending_notice.

A one-shot "flash message, but surviving past this request" - set by
the deferred publish/delete jobs right when they succeed, since by the
time they run, the request that triggered them is long gone and a real
flash_success() (session-based, same request only) has nothing to
attach to. Read-and-cleared the next time the dataset page renders.

Revision ID: 13515ec9f22d
Revises: d3f91a2b7c6e
Create Date: 2026-10-15 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = '13515ec9f22d'
down_revision = 'd3f91a2b7c6e'
branch_labels = None
depends_on = None


def _column_exists(table, column):
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    return column in [c['name'] for c in insp.get_columns(table)]


def upgrade():
    if not _column_exists('fair_zenodo_deposition', 'pending_notice'):
        op.add_column(
            'fair_zenodo_deposition',
            sa.Column('pending_notice', sa.UnicodeText, nullable=True),
        )


def downgrade():
    op.drop_column('fair_zenodo_deposition', 'pending_notice')
