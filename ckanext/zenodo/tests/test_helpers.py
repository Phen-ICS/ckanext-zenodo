"""DB-backed tests for the dataset-page template helper -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib.helpers import zenodo_deposition_for_package
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def test_no_deposition_returns_none():
    dataset = factories.Dataset()

    assert zenodo_deposition_for_package(dataset['id']) is None


def test_deposition_with_no_files_yet_returns_none():
    # Created but nothing uploaded yet - shouldn't be shown as "data
    # pending on Zenodo" when there's genuinely no file at all.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert zenodo_deposition_for_package(dataset['id']) is None


def test_pending_deposition_has_no_record_url():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    result = zenodo_deposition_for_package(dataset['id'])

    assert result['published'] is False
    assert result['record_url'] is None
    assert result['doi'] is None
    assert result['file_count'] == 1


def test_published_sandbox_deposition_has_a_record_url():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    result = zenodo_deposition_for_package(dataset['id'])

    assert result['published'] is True
    assert result['doi'] == '10.5281/zenodo.1'
    assert result['is_sandbox'] is True
    assert result['record_url'] == 'https://sandbox.zenodo.org/records/rec-1'


def test_published_production_deposition_points_at_production_host():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(
        dataset['id'], 'dep-1', 'https://example.test/bucket', is_sandbox=False
    )
    ZenodoDepositionQuery.increment_file_count(dataset['id'])
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    result = zenodo_deposition_for_package(dataset['id'])

    assert result['record_url'] == 'https://zenodo.org/records/rec-1'
