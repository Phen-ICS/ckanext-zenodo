"""DB-backed tests for the pending-deposition template helper -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib.helpers import zenodo_pending_deposition_for_package
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def test_no_deposition_is_not_pending():
    dataset = factories.Dataset()

    assert zenodo_pending_deposition_for_package(dataset['id']) is False


def test_deposition_with_no_files_yet_is_not_pending():
    # Created but nothing uploaded yet - there's genuinely nothing to
    # wait for, so this shouldn't claim there is.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert zenodo_pending_deposition_for_package(dataset['id']) is False


def test_unpublished_deposition_with_files_is_pending():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    assert zenodo_pending_deposition_for_package(dataset['id']) is True


def test_published_deposition_is_no_longer_pending():
    # The real CKAN resource exists for it by this point - showing this
    # notice too would be a confusing duplicate, not just unnecessary.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    assert zenodo_pending_deposition_for_package(dataset['id']) is False
