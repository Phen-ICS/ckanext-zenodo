"""DB-backed tests for the resource_delete auth override -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib.auth import resource_delete
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def _context(user):
    return {'user': user['name'], 'model': None, 'auth_user_obj': None}


def test_denies_deleting_the_last_file_of_a_published_deposition():
    user = factories.Sysadmin()
    org = factories.Organization(user=user)
    dataset = factories.Dataset(owner_org=org['id'])
    resource = factories.Resource(package_id=dataset['id'], name='only-file.txt')

    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'only-file.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    result = resource_delete(_context(user), {'id': resource['id']})

    assert result['success'] is False
    assert 'last file' in result['msg']


def test_allows_deleting_one_of_several_files():
    user = factories.Sysadmin()
    org = factories.Organization(user=user)
    dataset = factories.Dataset(owner_org=org['id'])
    resource = factories.Resource(package_id=dataset['id'], name='delete-me.txt')
    factories.Resource(package_id=dataset['id'], name='keep.txt')

    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'delete-me.txt')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    result = resource_delete(_context(user), {'id': resource['id']})

    assert result['success'] is True


def test_allows_deleting_an_unrelated_resource():
    user = factories.Sysadmin()
    org = factories.Organization(user=user)
    dataset = factories.Dataset(owner_org=org['id'])
    resource = factories.Resource(package_id=dataset['id'], name='plain-upload.txt')

    result = resource_delete(_context(user), {'id': resource['id']})

    assert result['success'] is True


def test_allows_deleting_the_last_file_when_not_published_yet():
    user = factories.Sysadmin()
    org = factories.Organization(user=user)
    dataset = factories.Dataset(owner_org=org['id'])
    resource = factories.Resource(package_id=dataset['id'], name='only-file.txt')

    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'only-file.txt')

    result = resource_delete(_context(user), {'id': resource['id']})

    assert result['success'] is True
