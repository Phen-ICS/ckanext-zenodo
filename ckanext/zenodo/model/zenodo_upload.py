"""One row per file being uploaded in chunks (lib/chunked_upload.py): from
the first chunk received until the file is on Zenodo, then deleted. Holds
what's needed to resume, retry or clean up - the bytes themselves are in a
file under ckan.storage_path, never in the database.
"""

import uuid
from datetime import datetime, timezone

from ckan.model import Session, meta
from ckan.model.domain_object import DomainObject
from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Integer, Table, types


def _utcnow():
    return datetime.now(timezone.utc)


# receiving -> queued -> sending -> (row deleted once on Zenodo)
#                           \-> failed -> queued (retry) | row deleted (cancel)
RECEIVING = 'receiving'
QUEUED = 'queued'
SENDING = 'sending'
FAILED = 'failed'

zenodo_upload_table = Table(
    'fair_zenodo_upload',
    meta.metadata,
    Column('id', types.UnicodeText, primary_key=True),
    Column(
        'package_id',
        types.UnicodeText,
        ForeignKey('package.id', onupdate='CASCADE', ondelete='CASCADE'),
        nullable=False,
    ),
    Column('user_id', types.UnicodeText, nullable=False),
    Column('filename', types.UnicodeText, nullable=False),
    Column('size', BigInteger, nullable=False),
    Column('received', BigInteger, nullable=False, default=0),
    Column('forced', Boolean, nullable=False, default=False),
    Column('status', types.UnicodeText, nullable=False, default=RECEIVING),
    # Why the last send to Zenodo failed, shown to the dataset's editors.
    Column('error', types.UnicodeText, nullable=True),
    Column('send_attempts', Integer, nullable=False, default=0),
    Column('created', types.DateTime, nullable=False, default=_utcnow),
    Column(
        'updated', types.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    ),
)


class ZenodoUpload(DomainObject):
    """A file on its way to Zenodo."""


meta.registry.map_imperatively(ZenodoUpload, zenodo_upload_table)


class ZenodoUploadQuery:
    @classmethod
    def create(cls, package_id, user_id, filename, size, forced):
        upload = ZenodoUpload(
            id=str(uuid.uuid4()),
            package_id=package_id,
            user_id=user_id,
            filename=filename,
            size=size,
            received=0,
            forced=forced,
            status=RECEIVING,
            send_attempts=0,
        )
        Session.add(upload)
        Session.flush()
        return upload

    @classmethod
    def get(cls, upload_id):
        return Session.get(ZenodoUpload, upload_id)

    @classmethod
    def for_package(cls, package_id):
        return (
            Session.query(ZenodoUpload)
            .filter(ZenodoUpload.package_id == package_id)
            .order_by(ZenodoUpload.created)
            .all()
        )

    @classmethod
    def all(cls):
        return Session.query(ZenodoUpload).all()

    @classmethod
    def update(cls, upload, **kwargs):
        for key, value in kwargs.items():
            setattr(upload, key, value)
        # onupdate only fires on a real UPDATE; touching a row with the
        # same values must still count as activity for the cleanup rules.
        upload.updated = _utcnow()
        Session.flush()

    @classmethod
    def delete(cls, upload):
        Session.delete(upload)
        Session.flush()
