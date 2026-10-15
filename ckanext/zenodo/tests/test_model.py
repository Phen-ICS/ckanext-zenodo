"""DB-backed tests for fair_zenodo_deposition - needs a real CKAN DB, so
--ckan-ini=/plugins/ckanext-zenodo/test.ini is required (the conftest.py
guard enforces this)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def test_create_and_read_package():
    dataset = factories.Dataset()

    record = ZenodoDepositionQuery.create(
        dataset['id'], 'dep-123', 'https://sandbox.zenodo.org/bucket/abc'
    )

    assert record.package_id == dataset['id']
    assert record.zenodo_deposition_id == 'dep-123'
    assert record.published is False
    assert record.file_count == 0
    assert record.is_sandbox is True

    fetched = ZenodoDepositionQuery.read_package(dataset['id'])
    assert fetched.zenodo_deposition_id == 'dep-123'


def test_read_package_returns_none_when_no_deposition():
    dataset = factories.Dataset()

    assert ZenodoDepositionQuery.read_package(dataset['id']) is None


def test_get_or_create_open_deposition_is_idempotent():
    dataset = factories.Dataset()
    calls = []

    def create_deposition_fn():
        calls.append(1)
        return 'dep-456', 'https://sandbox.zenodo.org/bucket/xyz'

    first = ZenodoDepositionQuery.get_or_create_open_deposition(
        dataset['id'], create_deposition_fn
    )
    second = ZenodoDepositionQuery.get_or_create_open_deposition(
        dataset['id'], create_deposition_fn
    )

    assert first.package_id == second.package_id
    assert first.zenodo_deposition_id == second.zenodo_deposition_id == 'dep-456'
    assert len(calls) == 1, (
        'create_deposition_fn should only run once - the second call must reuse '
        'the existing open deposition'
    )


def test_increment_file_count():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    ZenodoDepositionQuery.increment_file_count(dataset['id'])
    updated = ZenodoDepositionQuery.increment_file_count(dataset['id'])

    assert updated.file_count == 2


def test_increment_file_count_on_missing_deposition_returns_none():
    dataset = factories.Dataset()

    assert ZenodoDepositionQuery.increment_file_count(dataset['id']) is None


def test_add_file_records_filename_and_increments_count():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')
    updated = ZenodoDepositionQuery.add_file(dataset['id'], 'notes.txt')

    assert updated.file_count == 2
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == [
        'photo.jpg',
        'notes.txt',
    ]


def test_add_file_on_missing_deposition_returns_none():
    dataset = factories.Dataset()

    assert ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg') is None


def test_list_filenames_defaults_to_empty():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == []


def test_list_filenames_without_a_deposition_is_empty():
    dataset = factories.Dataset()

    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == []


def test_mark_published():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    published = ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1'
    )

    assert published.published is True
    assert published.zenodo_record_id == 'rec-1'
    assert published.zenodo_record_doi == '10.5281/zenodo.1'


def test_set_and_pop_notice():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    ZenodoDepositionQuery.set_notice(dataset['id'], 'published')

    assert ZenodoDepositionQuery.pop_notice(dataset['id']) == 'published'
    # Only shows once - a second pop finds nothing left.
    assert ZenodoDepositionQuery.pop_notice(dataset['id']) is None


def test_pop_notice_commits_the_clear_so_it_survives_a_rollback():
    """Regression test for a real bug found via live HTTP testing:
    pop_notice is called from a template helper during a plain
    dataset-page GET, which never goes through CKAN's own
    commit_and_remove() the way an action call does. A flush-only clear
    was only ever visible within that one request's own transaction and
    got silently rolled back once the request ended, so the very next,
    separate pageview re-read the same still-set notice and showed it
    again forever. Simulating "a later request's fresh read" here via
    an explicit Session.rollback() right after - if the clear wasn't
    really committed, this would resurrect 'published'."""
    from ckan.model import Session

    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.set_notice(dataset['id'], 'published')
    Session.commit()

    assert ZenodoDepositionQuery.pop_notice(dataset['id']) == 'published'
    Session.rollback()

    assert ZenodoDepositionQuery.read_package(dataset['id']).pending_notice is None


def test_pop_notice_without_one_set_is_none():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert ZenodoDepositionQuery.pop_notice(dataset['id']) is None


def test_pop_notice_without_a_deposition_is_none():
    dataset = factories.Dataset()

    assert ZenodoDepositionQuery.pop_notice(dataset['id']) is None


def test_delete_package():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert ZenodoDepositionQuery.delete_package(dataset['id']) is True
    assert ZenodoDepositionQuery.read_package(dataset['id']) is None
    assert ZenodoDepositionQuery.delete_package(dataset['id']) is False


def test_deposition_is_deleted_when_package_is_deleted():
    """FK is ON DELETE CASCADE - purging the dataset should take the
    deposition row with it, not leave an orphan."""
    from ckan.model import Session

    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    from ckan.tests.helpers import call_action

    call_action('package_delete', {'ignore_auth': True}, id=dataset['id'])
    call_action('dataset_purge', {'ignore_auth': True}, id=dataset['id'])
    Session.expire_all()

    assert ZenodoDepositionQuery.read_package(dataset['id']) is None
