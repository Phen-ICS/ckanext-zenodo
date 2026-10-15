"""Add concept_record_id.

zenodo_record_id is the *version's own* record id - it changes every
time a new version is published (see lib/delete_zenodo_file.py);
concept_record_id never changes across versions of the same deposition,
and is what CKAN resources actually point at (see publish_guard.py's
_zenodo_record_url, confirmed empirically that it always redirects to
whichever version is latest). Stored separately so a later
version-matching lookup ("is this CKAN resource one of ours?" in
delete_zenodo_file.py) doesn't need to compare against a value that was
only ever correct for one specific version.

Revision ID: d3f91a2b7c6e
Revises: acf4669d756f
Create Date: 2026-10-15 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = 'd3f91a2b7c6e'
down_revision = 'acf4669d756f'
branch_labels = None
depends_on = None


def _column_exists(table, column):
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    return column in [c['name'] for c in insp.get_columns(table)]


def upgrade():
    if not _column_exists('fair_zenodo_deposition', 'concept_record_id'):
        op.add_column(
            'fair_zenodo_deposition',
            sa.Column('concept_record_id', sa.UnicodeText, nullable=True),
        )


def downgrade():
    op.drop_column('fair_zenodo_deposition', 'concept_record_id')
