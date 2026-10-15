"""DB-backed tests for the pending-deposition template helper -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib.helpers import (
    zenodo_notice_for_package,
    zenodo_pending_deposition_for_package,
)
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def test_no_deposition_is_not_pending():
    dataset = factories.Dataset()

    assert zenodo_pending_deposition_for_package(dataset) is None


def test_deposition_with_no_files_yet_is_not_pending():
    # Created but nothing uploaded yet - there's genuinely nothing to
    # wait for, so this shouldn't claim there is.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert zenodo_pending_deposition_for_package(dataset) is None


def test_unpublished_deposition_on_a_public_dataset_is_waiting_on_the_job():
    dataset = factories.Dataset(private=False)
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')

    assert zenodo_pending_deposition_for_package(dataset) == {
        'waiting_for_public': False,
        'filenames': ['photo.jpg'],
    }


def test_unpublished_deposition_on_a_private_dataset_is_waiting_on_visibility():
    # The publish job isn't even enqueued until the dataset is public
    # (see publish_guard.maybe_publish_pending_deposition) - "reload in
    # a moment" would be actively misleading here.
    org = factories.Organization()
    dataset = factories.Dataset(private=True, owner_org=org['id'])
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')
    ZenodoDepositionQuery.add_file(dataset['id'], 'notes.txt')

    assert zenodo_pending_deposition_for_package(dataset) == {
        'waiting_for_public': True,
        'filenames': ['photo.jpg', 'notes.txt'],
    }


def test_published_deposition_is_no_longer_pending():
    # Real CKAN resources exist for it by this point - showing this
    # notice too would be a confusing duplicate, not just unnecessary.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    assert zenodo_pending_deposition_for_package(dataset) is None


def test_notice_for_package_none_by_default():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')

    assert zenodo_notice_for_package(dataset) is None


def test_notice_for_package_published_includes_the_doi():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')
    ZenodoDepositionQuery.set_notice(dataset['id'], 'published')

    result = zenodo_notice_for_package(dataset)

    assert result == {'message': 'Archived on Zenodo (DOI: 10.5281/zenodo.1).'}
    # Shows only once - a second render finds nothing left to show.
    assert zenodo_notice_for_package(dataset) is None


def test_notice_for_package_new_version_published():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-2', '10.5281/zenodo.2')
    ZenodoDepositionQuery.set_notice(dataset['id'], 'new_version_published')

    result = zenodo_notice_for_package(dataset)

    assert result == {'message': 'Zenodo archive updated (DOI: 10.5281/zenodo.2).'}
