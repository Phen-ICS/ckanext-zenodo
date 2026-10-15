"""DB-backed tests for the IXloader.can_upload hook -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard).

Tests the plain function directly (ckanext-xloader itself isn't
installed in this CI pipeline - see plugin.py's own soft-dependency
handling), same approach publish_guard.py's own tests use for a
function that's really an IPackageController/IResourceController hook
body."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib import xloader_guard
from ckanext.zenodo.lib.publish_guard import _zenodo_record_url
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def test_rejects_a_published_zenodo_backed_resource():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'results.csv')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    record_url = _zenodo_record_url('concept-1', True)
    resource = factories.Resource(
        package_id=dataset['id'],
        name='results.csv',
        url=f'{record_url}?preview_file=results.csv',
        format='CSV',
    )

    assert xloader_guard.can_upload(resource['id']) is False


def test_allows_an_unrelated_resource_with_the_same_name():
    # Same filename, but never recorded against this package's
    # deposition and a totally different url - must not be mistaken
    # for one of ours.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    resource = factories.Resource(
        package_id=dataset['id'],
        name='results.csv',
        url='https://example.test/real-upload/results.csv',
        format='CSV',
    )

    assert xloader_guard.can_upload(resource['id']) is True


def test_allows_a_resource_when_theres_no_deposition_at_all():
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], name='results.csv')

    assert xloader_guard.can_upload(resource['id']) is True


def test_allows_a_resource_matching_by_name_but_not_by_url():
    # Right filename, right package, but the deposition's own concept
    # record url doesn't match this resource's url - e.g. a real CKAN
    # upload that happens to share a name with a Zenodo-uploaded file.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'results.csv')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    resource = factories.Resource(
        package_id=dataset['id'],
        name='results.csv',
        url='https://example.test/some-other-upload/results.csv',
        format='CSV',
    )

    assert xloader_guard.can_upload(resource['id']) is True


def test_allows_a_resource_not_tracked_in_the_deposition_filenames():
    # Matches the concept record's own base url, but this particular
    # filename was never recorded as having actually gone to Zenodo
    # (e.g. list_filenames doesn't include it) - shouldn't happen in
    # practice, but err on the side of letting xloader try rather than
    # silently swallowing a resource this plugin doesn't actually own.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    record_url = _zenodo_record_url('concept-1', True)
    resource = factories.Resource(
        package_id=dataset['id'],
        name='results.csv',
        url=f'{record_url}?preview_file=results.csv',
        format='CSV',
    )

    assert xloader_guard.can_upload(resource['id']) is True


def test_allows_a_resource_that_does_not_exist():
    assert xloader_guard.can_upload('does-not-exist') is True
