"""DB-backed tests for the draft->active publish hook -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard).
toolkit.enqueue_job and zenodo_client are both monkeypatched - no real
RQ job, no real network."""

import pytest
from ckan.plugins import toolkit
from ckan.tests import factories

from ckanext.zenodo.lib import publish_guard, zenodo_client
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(
        publish_guard.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: calls.append((fn, kwargs)),
    )
    return calls


def _pkg_dict(package_id, state='active', type_='dataset', private=False):
    return {'id': package_id, 'type': type_, 'state': state, 'private': private}


def test_non_dataset_type_is_ignored(enqueued):
    publish_guard.maybe_publish_pending_deposition(_pkg_dict('pkg-1', type_='harvest'))

    assert enqueued == []


def test_non_active_state_is_ignored(enqueued):
    dataset = factories.Dataset(state='draft')
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    publish_guard.maybe_publish_pending_deposition(
        _pkg_dict(dataset['id'], state='draft')
    )

    assert enqueued == []


def test_no_deposition_is_ignored(enqueued):
    dataset = factories.Dataset()

    publish_guard.maybe_publish_pending_deposition(_pkg_dict(dataset['id']))

    assert enqueued == []


def test_already_published_deposition_is_ignored(enqueued):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    publish_guard.maybe_publish_pending_deposition(_pkg_dict(dataset['id']))

    assert enqueued == []


def test_empty_deposition_is_never_published(enqueued):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    # file_count stays 0 - no file was ever actually routed to Zenodo.

    publish_guard.maybe_publish_pending_deposition(_pkg_dict(dataset['id']))

    assert enqueued == []


def test_private_active_dataset_is_not_published_yet(enqueued):
    # Same trigger condition as ckanext-doi's own after_dataset_update:
    # active AND not private, not just active - a DOI (Zenodo's same as
    # DataCite's) is a one-way action, so a dataset that's active but
    # still private should never get one yet.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    publish_guard.maybe_publish_pending_deposition(
        _pkg_dict(dataset['id'], private=True)
    )

    assert enqueued == []


def test_dataset_made_public_later_gets_published_then(enqueued):
    # The hook fires on every update, not just draft -> active - a
    # dataset created active+private and made public only later must
    # still get published the next time it's updated, not be stuck
    # forever just because the publish job never ran right after
    # creation.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    publish_guard.maybe_publish_pending_deposition(
        _pkg_dict(dataset['id'], private=True)
    )
    assert enqueued == []

    publish_guard.maybe_publish_pending_deposition(
        _pkg_dict(dataset['id'], private=False)
    )
    assert len(enqueued) == 1


def test_pending_deposition_with_files_is_enqueued(enqueued):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    publish_guard.maybe_publish_pending_deposition(_pkg_dict(dataset['id']))

    assert len(enqueued) == 1
    fn, kwargs = enqueued[0]
    assert fn is publish_guard._publish_deposition
    assert kwargs == {'package_id': dataset['id']}


def test_extract_datacite_metadata_with_no_fdf_extra_is_empty():
    assert publish_guard._extract_datacite_metadata({'extras': []}) == {}
    assert publish_guard._extract_datacite_metadata({}) == {}


def test_extract_datacite_metadata_with_malformed_json_is_empty():
    pkg_dict = {'extras': [{'key': 'fdf_output_json', 'value': 'not json'}]}

    assert publish_guard._extract_datacite_metadata(pkg_dict) == {}


def test_extract_datacite_metadata_uses_real_fair3r_converter():
    """ckanext-fair3r is genuinely installed in this dev environment
    (same container, same /plugins mount) - exercising the real
    DataCiteConverter here is a useful sanity check that this module's
    assumption about its output shape still holds, on top of being the
    realistic case (fair3r is a soft dependency in code, but an
    operational sibling in practice). The ImportError fallback path
    itself isn't independently unit-tested here for the same reason -
    there's no way to make fair3r genuinely unimportable in this
    environment without breaking every other test in the suite."""
    import json

    fdf_output_json = json.dumps(
        {'creators': [{'givenName': 'Jane', 'familyName': 'Doe'}]}
    )

    metadata = publish_guard._extract_datacite_metadata(
        {'extras': [{'key': 'fdf_output_json', 'value': fdf_output_json}]}
    )

    assert metadata['creators'] == [
        {
            'nameType': 'Personal',
            'name': 'Doe, Jane',
            'givenName': 'Jane',
            'familyName': 'Doe',
        }
    ]


def test_submitter_display_name_from_creator_user():
    user = factories.User(fullname='Jane Researcher')

    name = publish_guard._submitter_display_name({'creator_user_id': user['id']})

    assert name == 'Jane Researcher'


def test_submitter_display_name_falls_back_when_user_missing():
    name = publish_guard._submitter_display_name({'creator_user_id': 'does-not-exist'})

    assert name == 'a FAIR3R user'


def test_submitter_display_name_falls_back_to_author_field():
    name = publish_guard._submitter_display_name({'author': 'J. Smith'})

    assert name == 'J. Smith'


def test_publish_deposition_marks_published_on_success(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    monkeypatch.setattr(
        zenodo_client,
        'update_deposition_metadata',
        lambda *a, **k: {'id': 'dep-1'},
    )
    monkeypatch.setattr(
        zenodo_client,
        'publish_deposition',
        lambda *a, **k: {
            'id': 'rec-1',
            'doi': '10.5281/zenodo.1',
            'files': [{'filename': 'results.csv'}],
        },
    )
    monkeypatch.setattr(
        zenodo_client, 'list_record_requests', lambda *a, **k: {'hits': {'hits': []}}
    )

    publish_guard._publish_deposition(dataset['id'])

    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.published is True
    assert record.zenodo_record_id == 'rec-1'
    assert record.zenodo_record_doi == '10.5281/zenodo.1'

    pkg = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    resources = pkg['resources']
    assert len(resources) == 1
    assert resources[0]['url'] == 'https://sandbox.zenodo.org/records/rec-1'
    # The resource is named after the real uploaded file, not a generic
    # "Archived on Zenodo" - see test_create_zenodo_resources_* below.
    assert resources[0]['name'] == 'results.csv'
    assert '10.5281/zenodo.1' in resources[0]['description']


def test_publish_deposition_leaves_unpublished_on_zenodo_error(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    def failing_publish(*a, **k):
        raise zenodo_client.ZenodoError('boom')

    monkeypatch.setattr(zenodo_client, 'update_deposition_metadata', lambda *a, **k: {})
    monkeypatch.setattr(zenodo_client, 'publish_deposition', failing_publish)

    publish_guard._publish_deposition(dataset['id'])

    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.published is False


def test_publish_deposition_is_a_noop_when_already_published(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    calls = []
    monkeypatch.setattr(
        zenodo_client,
        'publish_deposition',
        lambda *a, **k: calls.append(1),
    )

    publish_guard._publish_deposition(dataset['id'])

    assert calls == []


def test_publish_deposition_handles_missing_package():
    publish_guard._publish_deposition('does-not-exist')


def test_create_zenodo_resources_one_per_file():
    dataset = factories.Dataset()
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    files = [{'filename': 'photo.jpg'}, {'filename': 'notes.txt'}]

    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, files
    )

    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    resources = {r['name']: r for r in pkg_dict['resources']}
    assert set(resources) == {'photo.jpg', 'notes.txt'}
    # Same deposition/record - same landing page URL for every file.
    assert (
        resources['photo.jpg']['url']
        == resources['notes.txt']['url']
        == 'https://sandbox.zenodo.org/records/rec-1'
    )


def test_create_zenodo_resources_acts_as_the_site_user(monkeypatch):
    # Regression test: resource_create -> package_update fires CKAN's
    # activity-tracking signal, which needs context['user'] to resolve to
    # a *real* user - plain ignore_auth alone raised
    # ValidationError({'user_id': ['User not found']}) from deep inside
    # resource_create when this ran as an RQ job (no web request, no
    # "current user" at all), confirmed against the real DEV container.
    # Checked here by intercepting resource_create's own context rather
    # than round-tripping through the activity table - this test
    # environment's `activity` table schema is missing a column core
    # CKAN's current Activity model expects (a pre-existing, unrelated
    # test-DB migration gap), so querying it directly isn't reliable here.
    dataset = factories.Dataset()
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    site_user = toolkit.get_action('get_site_user')({'ignore_auth': True}, {})

    real_get_action = toolkit.get_action
    captured = {}

    def spying_get_action(name):
        action = real_get_action(name)
        if name != 'resource_create':
            return action

        def _spy(context, data_dict, **kw):
            captured['user'] = context.get('user')
            return action(context, data_dict, **kw)

        return _spy

    monkeypatch.setattr(publish_guard.toolkit, 'get_action', spying_get_action)

    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, [{'filename': 'photo.jpg'}]
    )

    assert captured.get('user') == site_user['name']


def test_create_zenodo_resources_idempotent_by_name():
    dataset = factories.Dataset()
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    files = [{'filename': 'photo.jpg'}]

    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, files
    )
    # Simulates a retried publish job: pkg_dict now reflects the resource
    # the first call already created.
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, files
    )

    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    assert len(pkg_dict['resources']) == 1


def test_create_zenodo_resources_noop_without_a_record_id():
    dataset = factories.Dataset()
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )

    publish_guard._create_zenodo_resources(
        pkg_dict, None, '10.5281/zenodo.1', True, [{'filename': 'photo.jpg'}]
    )

    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    assert pkg_dict['resources'] == []


def test_create_zenodo_resources_noop_without_files():
    dataset = factories.Dataset()
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )

    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, []
    )
    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, None
    )

    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )
    assert pkg_dict['resources'] == []


def test_create_zenodo_resources_swallows_failures(monkeypatch):
    dataset = factories.Dataset()
    pkg_dict = toolkit.get_action('package_show')(
        {'ignore_auth': True}, {'id': dataset['id']}
    )

    def failing_get_action(name):
        def _boom(*a, **k):
            raise toolkit.ValidationError({'url': ['boom']})

        return _boom

    monkeypatch.setattr(publish_guard.toolkit, 'get_action', failing_get_action)

    # Must not raise - a failure here never undoes the already-minted DOI.
    publish_guard._create_zenodo_resources(
        pkg_dict, 'rec-1', '10.5281/zenodo.1', True, [{'filename': 'photo.jpg'}]
    )


def test_accept_pending_community_inclusion_accepts_the_open_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        zenodo_client,
        'list_record_requests',
        lambda *a, **k: {
            'hits': {
                'hits': [
                    {'id': 'other-req', 'type': 'access', 'is_open': True},
                    {
                        'id': 'req-1',
                        'type': 'community-inclusion',
                        'is_open': True,
                    },
                ]
            }
        },
    )
    monkeypatch.setattr(
        zenodo_client,
        'accept_request',
        lambda request_id, *a, **k: calls.append(request_id),
    )

    publish_guard._accept_pending_community_inclusion('rec-1', 'tok', True, 'pkg-1')

    assert calls == ['req-1']


def test_accept_pending_community_inclusion_noop_when_none_open(monkeypatch):
    monkeypatch.setattr(
        zenodo_client,
        'list_record_requests',
        lambda *a, **k: {'hits': {'hits': []}},
    )
    calls = []
    monkeypatch.setattr(
        zenodo_client, 'accept_request', lambda *a, **k: calls.append(1)
    )

    publish_guard._accept_pending_community_inclusion('rec-1', 'tok', True, 'pkg-1')

    assert calls == []


def test_accept_pending_community_inclusion_noop_without_record_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        zenodo_client, 'list_record_requests', lambda *a, **k: calls.append(1)
    )

    publish_guard._accept_pending_community_inclusion(None, 'tok', True, 'pkg-1')

    assert calls == []


def test_accept_pending_community_inclusion_swallows_zenodo_error(monkeypatch):
    def failing_list(*a, **k):
        raise zenodo_client.ZenodoError('boom')

    monkeypatch.setattr(zenodo_client, 'list_record_requests', failing_list)

    # Must not raise - a failure here never rolls back the already-minted
    # DOI/publish.
    publish_guard._accept_pending_community_inclusion('rec-1', 'tok', True, 'pkg-1')
    # No exception raised - that's the whole assertion.


@pytest.mark.usefixtures('with_plugins')
def test_hook_fires_on_a_real_draft_to_active_transition(enqueued):
    """End-to-end: confirms IPackageController.after_dataset_update is
    actually wired and dispatches to this plugin's hook on a genuine
    CKAN state transition - not just that the function works when called
    directly, which every other test above already covers."""
    from ckan.tests.helpers import call_action

    org = factories.Organization()
    dataset = factories.Dataset(private=False, owner_org=org['id'], state='draft')
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.increment_file_count(dataset['id'])

    call_action(
        'package_patch', {'ignore_auth': True}, id=dataset['id'], state='active'
    )

    assert len(enqueued) == 1
    _fn, kwargs = enqueued[0]
    assert kwargs == {'package_id': dataset['id']}
