"""One row per dataset that has at least one file routed to Zenodo - the
deposition it's being (or was) built in. `package_id` is the primary key
directly (not a separate UUID id), same choice ckanext-citations'
fair_citation_stats table makes for the same reason: there is at most one
open/published deposition per dataset, so there's nothing a separate id
would add.
"""

from datetime import datetime, timezone

from ckan.model import Package, meta
from ckan.model.domain_object import DomainObject
from sqlalchemy import Boolean, Column, ForeignKey, Integer, Table, types
from sqlalchemy.orm import backref, relationship


def _utcnow():
    return datetime.now(timezone.utc)


zenodo_deposition_table = Table(
    'fair_zenodo_deposition',
    meta.metadata,
    Column(
        'package_id',
        types.UnicodeText,
        ForeignKey('package.id', onupdate='CASCADE', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column('zenodo_deposition_id', types.UnicodeText, nullable=False),
    Column('bucket_url', types.UnicodeText, nullable=False),
    # Only set once the deposition is actually published (DOI minted).
    Column('zenodo_record_id', types.UnicodeText, nullable=True),
    Column('zenodo_record_doi', types.UnicodeText, nullable=True),
    Column('is_sandbox', Boolean, nullable=False, default=True),
    Column('published', Boolean, nullable=False, default=False),
    Column('file_count', Integer, nullable=False, default=0),
    # Hash of the metadata dict last PUT to Zenodo for this deposition -
    # avoids re-sending identical metadata if the publish hook fires more
    # than once for the same draft->active transition.
    Column('last_metadata_hash', types.UnicodeText, nullable=True),
    Column('created', types.DateTime, nullable=False, default=_utcnow),
    Column(
        'updated', types.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    ),
)


class ZenodoDeposition(DomainObject):
    """A dataset's Zenodo deposition (open, or already published)."""


meta.registry.map_imperatively(
    ZenodoDeposition,
    zenodo_deposition_table,
    properties={
        'dataset': relationship(
            Package,
            backref=backref(
                'zenodo_deposition', uselist=False, cascade='all, delete-orphan'
            ),
            primaryjoin=zenodo_deposition_table.c.package_id.__eq__(Package.id),
        )
    },
)
