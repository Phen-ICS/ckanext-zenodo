"""Add filenames.

Lets the dataset page list the real filenames already uploaded to
Zenodo while the deposition is still unpublished (e.g. the dataset is
private - see publish_guard.py's active+public publish trigger) -
without this, there was nowhere to read them back from except a live
Zenodo API call on every page render, which is too slow/fragile to do
just to render a "here's what's waiting" notice.

Revision ID: acf4669d756f
Revises: bc8cf8f1144e
Create Date: 2026-10-15 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = 'acf4669d756f'
down_revision = 'bc8cf8f1144e'
branch_labels = None
depends_on = None


def _column_exists(table, column):
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    return column in [c['name'] for c in insp.get_columns(table)]


def upgrade():
    if not _column_exists('fair_zenodo_deposition', 'filenames'):
        op.add_column(
            'fair_zenodo_deposition',
            # JSON-encoded list of strings, not a native array/JSON
            # column type - kept deliberately simple (this is a display
            # convenience, never queried into), consistent with this
            # table's other plain-text columns.
            sa.Column('filenames', sa.UnicodeText, nullable=False, server_default='[]'),
        )


def downgrade():
    op.drop_column('fair_zenodo_deposition', 'filenames')
