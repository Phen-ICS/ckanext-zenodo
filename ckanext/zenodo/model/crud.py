"""Convenience query helpers for fair_zenodo_deposition, mirroring
ckanext-doi's DOIQuery (same create/read_package/update_package/delete_package
shape). Session.get(Model, pk) rather than the legacy Query.get() - same
SQLAlchemy-2.0-clean style ckanext-citations' model layer already uses.
"""

import json
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
    def add_file(cls, package_id, filename):
        """Records a real uploaded filename, in addition to incrementing
        file_count - what the upload blueprint actually calls (rather
        than increment_file_count alone) so the dataset page can list
        real filenames while the deposition is still unpublished (see
        lib/helpers.py's zenodo_pending_deposition_for_package).

        A filename already recorded is left as is: re-uploading a file
        under the same name *replaces* it on Zenodo (bucket API PUT on the
        same key - confirmed on the sandbox, one file left with the second
        upload's content), so counting it again would leave file_count
        and the pending list one file too high."""
        record = cls.read_package(package_id)
        if record is None:
            return None
        try:
            filenames = json.loads(record.filenames or '[]')
        except ValueError:
            filenames = []
        if filename in filenames:
            return record
        filenames.append(filename)
        return cls.update_package(
            package_id,
            file_count=record.file_count + 1,
            filenames=json.dumps(filenames),
        )

    @classmethod
    def remove_file(cls, package_id, filename):
        """Reverse of add_file - removes one filename (if present) and
        decrements file_count to match, e.g. after the user deletes that
        file (see lib/delete_zenodo_file.py). A no-op (not an error) if
        the filename isn't actually in the list - deleting twice, or a
        retried job after a crash right after this step, must not go
        negative."""
        record = cls.read_package(package_id)
        if record is None:
            return None
        try:
            filenames = json.loads(record.filenames or '[]')
        except ValueError:
            filenames = []
        if filename not in filenames:
            return record
        filenames.remove(filename)
        return cls.update_package(
            package_id,
            file_count=max(record.file_count - 1, 0),
            filenames=json.dumps(filenames),
        )

    @classmethod
    def list_filenames(cls, package_id):
        """The real filenames recorded so far for this dataset's
        deposition, or [] if there's no deposition (or nothing uploaded
        to it) at all."""
        record = cls.read_package(package_id)
        if record is None:
            return []
        try:
            return json.loads(record.filenames or '[]')
        except ValueError:
            return []

    @classmethod
    def mark_published(
        cls, package_id, zenodo_record_id, zenodo_record_doi, concept_record_id=None
    ):
        kwargs = {
            'published': True,
            'zenodo_record_id': zenodo_record_id,
            'zenodo_record_doi': zenodo_record_doi,
        }
        if concept_record_id is not None:
            kwargs['concept_record_id'] = concept_record_id
        return cls.update_package(package_id, **kwargs)

    @classmethod
    def set_notice(cls, package_id, notice):
        """Sets the one-shot notice a deferred job wants shown on the
        dataset page's next render (see lib/helpers.py's
        zenodo_notice_for_package) - a plain string, this table's own
        business, not a DB column enum, so new notice kinds don't need
        a migration."""
        return cls.update_package(package_id, pending_notice=notice)

    @classmethod
    def pop_notice(cls, package_id):
        """Reads the pending notice and clears it in the same call, so
        it only ever shows once - returns None if there wasn't one (the
        common case).

        Explicitly commits the clearing write, unlike every other method
        here. Confirmed empirically: this is called from a template
        helper during a plain dataset-page GET, which - unlike an
        action call (resource_create, package_update, ...) - never goes
        through CKAN's own action-layer commit_and_remove(). Without a
        commit here, the UPDATE is only ever visible within this one
        request's own transaction (which is exactly why it still looked
        right at the time of the render) and gets silently rolled back
        once the request ends, so the very next pageview starts a fresh
        transaction, rereads the same still-'published' row, and shows
        the same notice again - forever, instead of once."""
        record = cls.read_package(package_id)
        if record is None or not record.pending_notice:
            return None
        notice = record.pending_notice
        cls.update_package(package_id, pending_notice=None)
        Session.commit()
        return notice

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
