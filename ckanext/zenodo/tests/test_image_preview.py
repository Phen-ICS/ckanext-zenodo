"""Tests for the Image-view sync - --ckan-ini=/plugins/ckanext-zenodo/test.ini
required (conftest.py guard) for the DB-backed ones.

The resource_view_* actions themselves are faked here (not exercised for
real) rather than depending on the real `image_view` plugin actually
being loaded by the test harness - confirmed unreliable in practice
(loading it depends on test *order* within a pytest session, for
reasons not fully understood). What matters for these tests is this
module's own branching (create vs. update vs. no-op), not CKAN core's
own view-plugin wiring, which is exercised for real in the DEV
container instead (see the commit message for how that was verified
against the real sandbox)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib import image_preview

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def _fake_view_actions(monkeypatch, initial_views=None):
    """Patches image_preview.toolkit.get_action so resource_view_list/
    create/update operate on a plain in-memory list instead of the real
    actions - returns that list so a test can inspect it directly."""
    views = list(initial_views or [])
    calls = {'create': 0, 'update': 0}

    def resource_view_list(context, data_dict):
        return [v for v in views if v['resource_id'] == data_dict['id']]

    def resource_view_create(context, data_dict):
        calls['create'] += 1
        view = {
            'id': f'view-{len(views) + 1}',
            'resource_id': data_dict['resource_id'],
            'view_type': data_dict['view_type'],
            'image_url': data_dict.get('image_url'),
        }
        views.append(view)
        return view

    def resource_view_update(context, data_dict):
        calls['update'] += 1
        for view in views:
            if view['id'] == data_dict['id']:
                view['image_url'] = data_dict.get('image_url')
                return view
        raise image_preview.toolkit.ObjectNotFound()

    actions = {
        'resource_view_list': resource_view_list,
        'resource_view_create': resource_view_create,
        'resource_view_update': resource_view_update,
    }

    def fake_get_action(name):
        return actions[name]

    monkeypatch.setattr(image_preview.toolkit, 'get_action', fake_get_action)
    return views, calls


def test_zenodo_file_content_url_sandbox():
    url = image_preview.zenodo_file_content_url('620695', True, 'photo.jpg')

    assert (
        url == 'https://sandbox.zenodo.org/api/records/620695/files/photo.jpg/content'
    )


def test_zenodo_file_content_url_production():
    url = image_preview.zenodo_file_content_url('620695', False, 'photo.jpg')

    assert url == 'https://zenodo.org/api/records/620695/files/photo.jpg/content'


def test_zenodo_file_content_url_encodes_the_filename():
    # Confirmed empirically against the real sandbox: a literal space and
    # parentheses in the filename must be percent-encoded in the path
    # (unlike the ?preview_file= *query string*, which uses quote_plus's
    # '+' for space - see publish_guard._zenodo_file_preview_url) - a
    # real request with %20/%28/%29 here was confirmed to work.
    url = image_preview.zenodo_file_content_url('620695', True, 'test (Copie).csv')

    assert url == (
        'https://sandbox.zenodo.org/api/records/620695/files/'
        'test%20%28Copie%29.csv/content'
    )


def test_sync_image_preview_noop_for_a_non_image_format(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='CSV')
    views, calls = _fake_view_actions(monkeypatch)

    image_preview.sync_image_preview(
        resource['id'], 'CSV', 'https://example.test/content', 'does-not-matter'
    )

    assert views == []
    assert calls == {'create': 0, 'update': 0}


def test_sync_image_preview_noop_without_a_content_url(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')
    views, calls = _fake_view_actions(monkeypatch)

    image_preview.sync_image_preview(resource['id'], 'PNG', None, 'does-not-matter')

    assert views == []
    assert calls == {'create': 0, 'update': 0}


def test_sync_image_preview_creates_a_view_when_none_exists(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')
    views, calls = _fake_view_actions(monkeypatch)
    content_url = 'https://sandbox.zenodo.org/api/records/1/files/a.png/content'

    image_preview.sync_image_preview(resource['id'], 'PNG', content_url, 'a-user')

    assert calls == {'create': 1, 'update': 0}
    assert len(views) == 1
    assert views[0]['view_type'] == 'image_view'
    assert views[0]['image_url'] == content_url


def test_sync_image_preview_updates_an_existing_view(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')
    existing = {
        'id': 'view-1',
        'resource_id': resource['id'],
        'view_type': 'image_view',
        'image_url': 'https://sandbox.zenodo.org/api/records/1/files/a.png/content',
    }
    views, calls = _fake_view_actions(monkeypatch, [existing])
    new_url = 'https://sandbox.zenodo.org/api/records/2/files/a.png/content'

    image_preview.sync_image_preview(resource['id'], 'PNG', new_url, 'a-user')

    assert calls == {'create': 0, 'update': 1}
    assert len(views) == 1
    assert views[0]['image_url'] == new_url


def test_sync_image_preview_is_a_noop_when_already_correct(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')
    content_url = 'https://sandbox.zenodo.org/api/records/1/files/a.png/content'
    existing = {
        'id': 'view-1',
        'resource_id': resource['id'],
        'view_type': 'image_view',
        'image_url': content_url,
    }
    views, calls = _fake_view_actions(monkeypatch, [existing])

    image_preview.sync_image_preview(resource['id'], 'PNG', content_url, 'a-user')

    assert calls == {'create': 0, 'update': 0}
    assert views == [existing]


def test_sync_image_preview_never_raises_on_action_failure(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')

    def failing_get_action(name):
        def _boom(*a, **k):
            raise image_preview.toolkit.ValidationError({'image_url': ['boom']})

        return _boom

    monkeypatch.setattr(image_preview.toolkit, 'get_action', failing_get_action)

    # Must not raise - a failed preview sync is cosmetic, never a reason
    # to fail the publish/republish job that called this.
    image_preview.sync_image_preview(
        resource['id'], 'PNG', 'https://example.test/a.png', 'a-user'
    )
