"""Add publish_attempts.

Publish jobs run since the last successful publish, so retry-pending can
stop re-queueing a deposition that keeps failing.

Revision ID: 5c7e9a2b4d10
Revises: 13515ec9f22d
Create Date: 2026-10-20 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = '5c7e9a2b4d10'
down_revision = '13515ec9f22d'
branch_labels = None
depends_on = None


def _column_exists(table, column):
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    return column in [c['name'] for c in insp.get_columns(table)]


def upgrade():
    if not _column_exists('fair_zenodo_deposition', 'publish_attempts'):
        op.add_column(
            'fair_zenodo_deposition',
            sa.Column(
                'publish_attempts', sa.Integer, nullable=False, server_default='0'
            ),
        )


def downgrade():
    op.drop_column('fair_zenodo_deposition', 'publish_attempts')
