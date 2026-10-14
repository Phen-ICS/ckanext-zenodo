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


def test_mark_published():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    published = ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1'
    )

    assert published.published is True
    assert published.zenodo_record_id == 'rec-1'
    assert published.zenodo_record_doi == '10.5281/zenodo.1'


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
