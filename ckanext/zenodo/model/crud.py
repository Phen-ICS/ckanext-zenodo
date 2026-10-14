"""Convenience query helpers for fair_zenodo_deposition, mirroring
ckanext-doi's DOIQuery (same create/read_package/update_package/delete_package
shape). Session.get(Model, pk) rather than the legacy Query.get() - same
SQLAlchemy-2.0-clean style ckanext-citations' model layer already uses.
"""

from typing import ClassVar

from ckan.model import Session

from ckanext.zenodo.model.zenodo_deposition import (
    ZenodoDeposition,
    zenodo_deposition_table,
)


class ZenodoDepositionQuery:
    m = ZenodoDeposition
    cols: ClassVar[list[str]] = [c.name for c in zenodo_deposition_table.c]

    @classmethod
    def create(cls, package_id, zenodo_deposition_id, bucket_url, is_sandbox=True):
        """Create a new deposition record for a dataset that doesn't have
        one yet."""
        record = ZenodoDeposition(
            package_id=package_id,
            zenodo_deposition_id=zenodo_deposition_id,
            bucket_url=bucket_url,
            is_sandbox=is_sandbox,
            published=False,
            file_count=0,
        )
        Session.add(record)
        Session.flush()
        return record

    @classmethod
    def read_package(cls, package_id):
        """Retrieve the deposition record for a dataset, or None if it
        doesn't have one yet."""
        return Session.get(ZenodoDeposition, package_id)

    @classmethod
    def get_or_create_open_deposition(
        cls, package_id, create_deposition_fn, is_sandbox=True
    ):
        """Idempotent reuse: if a non-published deposition already exists
        for this dataset, return it as-is. Otherwise call
        `create_deposition_fn()` (expected to return (zenodo_deposition_id,
        bucket_url), e.g. by calling zenodo_client.create_deposition) and
        persist a new record. A *published* existing record is returned
        unchanged too - the caller decides what to do with an
        already-published dataset gaining another file (out of scope for
        this stage; see the implementation plan)."""
        existing = cls.read_package(package_id)
        if existing is not None:
            return existing

        zenodo_deposition_id, bucket_url = create_deposition_fn()
        return cls.create(package_id, zenodo_deposition_id, bucket_url, is_sandbox)

    @classmethod
    def update_package(cls, package_id, **kwargs):
        """Update whichever of this table's own columns are passed in.

        Fetches the row and sets attributes directly rather than a bulk
        Session.query(...).update(...) - confirmed directly that the bulk
        form writes the right values to the database but does not
        reliably refresh the already-loaded ORM object's own attributes
        for every column (synchronize_session='evaluate' silently missed
        some here), so a subsequent read_package() returned stale values
        even though the row itself was correct. Loading and mutating the
        object avoids that whole class of bug and is the more idiomatic
        pattern anyway for a single-row update by primary key.
        """
        record = cls.read_package(package_id)
        if record is None:
            return None
        for key, value in kwargs.items():
            if key in cls.cols:
                setattr(record, key, value)
        Session.flush()
        return record

    @classmethod
    def increment_file_count(cls, package_id):
        record = cls.read_package(package_id)
        if record is None:
            return None
        return cls.update_package(package_id, file_count=record.file_count + 1)

    @classmethod
    def mark_published(cls, package_id, zenodo_record_id, zenodo_record_doi):
        return cls.update_package(
            package_id,
            published=True,
            zenodo_record_id=zenodo_record_id,
            zenodo_record_doi=zenodo_record_doi,
        )

    @classmethod
    def delete_package(cls, package_id):
        """Delete the deposition record for a dataset. Returns True if a
        record was deleted, False if there wasn't one."""
        record = cls.read_package(package_id)
        if record is None:
            return False
        Session.delete(record)
        Session.flush()
        return True
