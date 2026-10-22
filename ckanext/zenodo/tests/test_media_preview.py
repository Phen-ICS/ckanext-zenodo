"""Tests for the Image/Audio/Video/PDF view sync -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py
guard) for the DB-backed ones.

The resource_view_* actions themselves are faked here (not exercised
for real) rather than depending on the real view plugins actually
being loaded by the test harness - confirmed unreliable in practice
for image_view (loading it depends on test *order* within a pytest
session, for reasons not fully understood). What matters for these
tests is this module's own branching (create vs. update vs. no-op,
and picking the right view kind for a format), not CKAN core's own
view-plugin wiring, which is exercised for real in the DEV container
instead (see the commit message for how that was verified against the
real sandbox)."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib import media_preview

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


def _fake_view_actions(monkeypatch, initial_views=None):
    """Patches media_preview.toolkit.get_action so resource_view_list/
    create/update operate on a plain in-memory list instead of the real
    actions - returns that list (plus a call counter) so a test can
    inspect them directly. Handles whichever of image_url/audio_url/
    video_url/pdf_url is actually passed, rather than hardcoding one."""
    views = list(initial_views or [])
    calls = {'create': 0, 'update': 0}
    url_fields = ('image_url', 'audio_url', 'video_url', 'pdf_url')

    def resource_view_list(context, data_dict):
        return [v for v in views if v['resource_id'] == data_dict['id']]

    def resource_view_create(context, data_dict):
        calls['create'] += 1
        view = {
            'id': f'view-{len(views) + 1}',
            'resource_id': data_dict['resource_id'],
            'view_type': data_dict['view_type'],
        }
        for field in url_fields:
            if field in data_dict:
                view[field] = data_dict[field]
        views.append(view)
        return view

    def resource_view_update(context, data_dict):
        calls['update'] += 1
        for view in views:
            if view['id'] == data_dict['id']:
                for field in url_fields:
                    if field in data_dict:
                        view[field] = data_dict[field]
                return view
        raise media_preview.toolkit.ObjectNotFound()

    def resource_view_delete(context, data_dict):
        calls['delete'] = calls.get('delete', 0) + 1
        views[:] = [v for v in views if v['id'] != data_dict['id']]

    actions = {
        'resource_view_list': resource_view_list,
        'resource_view_create': resource_view_create,
        'resource_view_update': resource_view_update,
        'resource_view_delete': resource_view_delete,
    }

    def fake_get_action(name):
        return actions[name]

    monkeypatch.setattr(media_preview.toolkit, 'get_action', fake_get_action)
    return views, calls


def test_zenodo_file_content_url_sandbox():
    url = media_preview.zenodo_file_content_url('620695', True, 'photo.jpg')

    assert (
        url == 'https://sandbox.zenodo.org/api/records/620695/files/photo.jpg/content'
    )


def test_zenodo_file_content_url_production():
    url = media_preview.zenodo_file_content_url('620695', False, 'photo.jpg')

    assert url == 'https://zenodo.org/api/records/620695/files/photo.jpg/content'


def test_zenodo_file_content_url_encodes_the_filename():
    # Confirmed empirically against the real sandbox: a literal space and
    # parentheses in the filename must be percent-encoded in the path
    # (unlike the ?preview_file= *query string*, which uses quote_plus's
    # '+' for space - see publish_guard._zenodo_file_preview_url) - a
    # real request with %20/%28/%29 here was confirmed to work.
    url = media_preview.zenodo_file_content_url('620695', True, 'test (Copie).csv')

    assert url == (
        'https://sandbox.zenodo.org/api/records/620695/files/'
        'test%20%28Copie%29.csv/content'
    )


def test_sync_media_preview_noop_for_an_unsupported_format(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='CSV')
    views, calls = _fake_view_actions(monkeypatch)

    media_preview.sync_media_preview(
        resource['id'], 'CSV', 'https://example.test/content', 'does-not-matter'
    )

    assert views == []
    assert calls == {'create': 0, 'update': 0}


def test_sync_media_preview_noop_without_a_content_url(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')
    views, calls = _fake_view_actions(monkeypatch)

    media_preview.sync_media_preview(resource['id'], 'PNG', None, 'does-not-matter')

    assert views == []
    assert calls == {'create': 0, 'update': 0}


@pytest.mark.parametrize(
    ('fmt', 'view_type', 'url_field'),
    [
        ('PNG', 'image_view', 'image_url'),
        ('MP3', 'audio_view', 'audio_url'),
        ('MP4', 'video_view', 'video_url'),
    ],
)
def test_sync_media_preview_creates_the_right_view_kind(
    monkeypatch, fmt, view_type, url_field
):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format=fmt)
    views, calls = _fake_view_actions(monkeypatch)
    content_url = 'https://sandbox.zenodo.org/api/records/1/files/a/content'

    media_preview.sync_media_preview(resource['id'], fmt, content_url, 'a-user')

    assert calls == {'create': 1, 'update': 0}
    assert len(views) == 1
    assert views[0]['view_type'] == view_type
    assert views[0][url_field] == content_url


def test_sync_media_preview_updates_an_existing_view(monkeypatch):
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

    media_preview.sync_media_preview(resource['id'], 'PNG', new_url, 'a-user')

    assert calls == {'create': 0, 'update': 1}
    assert len(views) == 1
    assert views[0]['image_url'] == new_url


def test_sync_media_preview_is_a_noop_when_already_correct(monkeypatch):
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

    media_preview.sync_media_preview(resource['id'], 'PNG', content_url, 'a-user')

    assert calls == {'create': 0, 'update': 0}
    assert views == [existing]


def test_sync_media_preview_never_raises_on_action_failure(monkeypatch):
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PNG')

    def failing_get_action(name):
        def _boom(*a, **k):
            raise media_preview.toolkit.ValidationError({'image_url': ['boom']})

        return _boom

    monkeypatch.setattr(media_preview.toolkit, 'get_action', failing_get_action)

    # Must not raise - a failed preview sync is cosmetic, never a reason
    # to fail the publish/republish job that called this.
    media_preview.sync_media_preview(
        resource['id'], 'PNG', 'https://example.test/a.png', 'a-user'
    )


def test_sync_media_preview_removes_the_pdf_view_ckan_creates(monkeypatch):
    # Zenodo serves files as non-embeddable downloads: a PDF view would only
    # show a blank box, so the one CKAN adds by default is removed.
    dataset = factories.Dataset()
    resource = factories.Resource(package_id=dataset['id'], format='PDF')
    auto_created = {
        'id': 'view-1',
        'resource_id': resource['id'],
        'view_type': 'pdf_view',
    }
    views, calls = _fake_view_actions(monkeypatch, [auto_created])

    media_preview.sync_media_preview(
        resource['id'],
        'PDF',
        'https://sandbox.zenodo.org/api/records/1/files/a.pdf/content',
        'a-user',
    )

    assert views == []
    assert calls == {'create': 0, 'update': 0, 'delete': 1}
