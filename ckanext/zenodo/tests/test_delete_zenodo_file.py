"""DB-backed tests for deleting a file from a dataset's Zenodo
deposition - --ckan-ini=/plugins/ckanext-zenodo/test.ini required
(conftest.py guard). zenodo_client is monkeypatched throughout - no real
network."""

import pytest
from ckan.tests import factories

from ckanext.zenodo.lib import delete_zenodo_file, publish_guard, zenodo_client
from ckanext.zenodo.model import ZenodoDepositionQuery, ZenodoUploadQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


# ----- delete_pending_file (before publication) -----


def test_delete_pending_file_removes_it_from_the_bucket_and_locally(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    ZenodoDepositionQuery.add_file(dataset['id'], 'delete-me.txt')

    monkeypatch.setattr(
        zenodo_client,
        'get_deposition',
        lambda *a, **k: {
            'files': [
                {'id': 'file-1', 'filename': 'keep.txt'},
                {'id': 'file-2', 'filename': 'delete-me.txt'},
            ]
        },
    )
    calls = []
    monkeypatch.setattr(
        zenodo_client,
        'delete_deposition_file',
        lambda dep_id, file_id, *a, **k: calls.append((dep_id, file_id)),
    )

    deleted = delete_zenodo_file.delete_pending_file(dataset['id'], 'delete-me.txt')

    assert deleted is True
    assert calls == [('dep-1', 'file-2')]
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['keep.txt']
    assert ZenodoDepositionQuery.read_package(dataset['id']).file_count == 1


def test_delete_pending_file_without_a_deposition_is_a_noop():
    dataset = factories.Dataset()

    assert delete_zenodo_file.delete_pending_file(dataset['id'], 'x.txt') is False


def test_delete_pending_file_on_a_published_deposition_is_a_noop():
    # That's the other function's job (maybe_delete_published_file) -
    # this one must never touch an already-published deposition.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    ZenodoDepositionQuery.mark_published(dataset['id'], 'rec-1', '10.5281/zenodo.1')

    assert delete_zenodo_file.delete_pending_file(dataset['id'], 'keep.txt') is False


def test_delete_pending_file_unknown_filename_is_a_noop():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')

    assert delete_zenodo_file.delete_pending_file(dataset['id'], 'nope.txt') is False


def test_delete_pending_file_still_untracks_it_if_zenodo_already_lost_it(monkeypatch):
    # e.g. a previous attempt succeeded on Zenodo's side but crashed
    # before the local bookkeeping update ran.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'delete-me.txt')

    monkeypatch.setattr(zenodo_client, 'get_deposition', lambda *a, **k: {'files': []})
    calls = []
    monkeypatch.setattr(
        zenodo_client, 'delete_deposition_file', lambda *a, **k: calls.append(1)
    )
    monkeypatch.setattr(zenodo_client, 'delete_deposition', lambda *a, **k: None)

    deleted = delete_zenodo_file.delete_pending_file(dataset['id'], 'delete-me.txt')

    assert deleted is True
    assert calls == [], 'no file id was found - nothing to call Zenodo delete with'
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == []


@pytest.fixture
def last_file(monkeypatch):
    """A never-published draft holding one file, and the Zenodo calls."""
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'only.txt')
    monkeypatch.setattr(
        zenodo_client,
        'get_deposition',
        lambda *a, **k: {'files': [{'id': 'file-1', 'filename': 'only.txt'}]},
    )
    monkeypatch.setattr(zenodo_client, 'delete_deposition_file', lambda *a, **k: None)
    deleted = []
    monkeypatch.setattr(
        zenodo_client,
        'delete_deposition',
        lambda dep_id, *a, **k: deleted.append(dep_id),
    )
    return dataset, deleted


def test_deleting_the_last_pending_file_deletes_the_empty_draft(last_file):
    dataset, deleted = last_file

    assert delete_zenodo_file.delete_pending_file(dataset['id'], 'only.txt') is True

    assert deleted == ['dep-1']
    assert ZenodoDepositionQuery.read_package(dataset['id']) is None


def test_the_draft_is_kept_while_an_upload_is_on_its_way(last_file):
    dataset, deleted = last_file
    user = factories.User()
    ZenodoUploadQuery.create(dataset['id'], user['id'], 'next.bin', 1, True)

    delete_zenodo_file.delete_pending_file(dataset['id'], 'only.txt')

    assert deleted == []
    assert ZenodoDepositionQuery.read_package(dataset['id']) is not None


# ----- maybe_delete_published_file / _delete_published_file (after publication) -----


def _published_dataset_with_resource():
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    ZenodoDepositionQuery.add_file(dataset['id'], 'delete-me.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    resource = {
        'id': 'res-1',
        'package_id': dataset['id'],
        'name': 'delete-me.txt',
        'url': publish_guard._zenodo_record_url('concept-1', True),
    }
    return dataset, resource


def test_maybe_delete_published_file_enqueues_for_a_matching_resource(monkeypatch):
    dataset, resource = _published_dataset_with_resource()
    calls = []
    monkeypatch.setattr(
        delete_zenodo_file.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: calls.append((fn, kwargs)),
    )

    context = {}
    delete_zenodo_file.maybe_delete_published_file(context, {'id': 'res-1'}, [resource])
    # Nothing yet: only once CKAN's own deletion went through.
    assert calls == []
    delete_zenodo_file.enqueue_pending_deletions(context, [])

    assert len(calls) == 1
    fn, kwargs = calls[0]
    assert fn is delete_zenodo_file._delete_published_file
    assert kwargs == {
        'package_id': dataset['id'],
        'filename': 'delete-me.txt',
        'resource_id': 'res-1',
    }


def test_nothing_enqueued_when_the_resource_was_not_deleted(monkeypatch):
    # e.g. resource_delete failed after before_resource_delete ran.
    _dataset, resource = _published_dataset_with_resource()
    calls = []
    monkeypatch.setattr(
        delete_zenodo_file.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: calls.append(1),
    )

    context = {}
    delete_zenodo_file.maybe_delete_published_file(context, {'id': 'res-1'}, [resource])
    delete_zenodo_file.enqueue_pending_deletions(context, [resource])

    assert calls == []


def test_resource_delete_enqueues_only_once_the_resource_is_gone(monkeypatch):
    from ckan.tests.helpers import call_action

    dataset, _resource = _published_dataset_with_resource()
    resource = factories.Resource(
        package_id=dataset['id'],
        name='delete-me.txt',
        url=publish_guard._zenodo_file_preview_url('concept-1', True, 'delete-me.txt'),
    )
    calls = []
    monkeypatch.setattr(
        delete_zenodo_file.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: calls.append(kwargs),
    )

    call_action('resource_delete', {'ignore_auth': True}, id=resource['id'])

    assert calls == [
        {
            'package_id': dataset['id'],
            'filename': 'delete-me.txt',
            'resource_id': resource['id'],
        }
    ]


def test_delete_published_file_leaves_zenodo_alone_if_the_resource_is_active(
    monkeypatch,
):
    dataset, _resource = _published_dataset_with_resource()
    resource = factories.Resource(package_id=dataset['id'], name='delete-me.txt')
    monkeypatch.setattr(
        zenodo_client,
        'create_new_version',
        lambda *a, **k: pytest.fail('Zenodo must not be touched'),
    )

    delete_zenodo_file._delete_published_file(
        dataset['id'], 'delete-me.txt', resource_id=resource['id']
    )

    assert 'delete-me.txt' in ZenodoDepositionQuery.list_filenames(dataset['id'])


def test_maybe_delete_published_file_ignores_an_unrelated_resource(monkeypatch):
    dataset, _resource = _published_dataset_with_resource()
    unrelated = {
        'id': 'res-2',
        'package_id': dataset['id'],
        'name': 'not-ours.txt',
        'url': 'https://example.test/not-zenodo',
    }
    calls = []
    monkeypatch.setattr(
        delete_zenodo_file.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: calls.append(1),
    )

    delete_zenodo_file.maybe_delete_published_file({}, {'id': 'res-2'}, [unrelated])

    assert calls == []


def test_maybe_delete_published_file_ignores_an_unpublished_deposition(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    resource = {
        'id': 'res-1',
        'package_id': dataset['id'],
        'name': 'keep.txt',
        'url': 'https://sandbox.zenodo.org/records/whatever',
    }
    calls = []
    monkeypatch.setattr(
        delete_zenodo_file.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: calls.append(1),
    )

    delete_zenodo_file.maybe_delete_published_file({}, {'id': 'res-1'}, [resource])

    assert calls == []


def test_delete_published_file_publishes_a_new_version_without_it(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    ZenodoDepositionQuery.add_file(dataset['id'], 'delete-me.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    monkeypatch.setattr(
        zenodo_client,
        'create_new_version',
        lambda *a, **k: {'id': 'new-draft-1', 'links': {'bucket': 'https://x/bucket'}},
    )
    monkeypatch.setattr(
        zenodo_client,
        'get_deposition',
        lambda *a, **k: {'files': [{'id': 'file-2', 'filename': 'delete-me.txt'}]},
    )
    delete_calls = []
    monkeypatch.setattr(
        zenodo_client,
        'delete_deposition_file',
        lambda dep_id, file_id, *a, **k: delete_calls.append((dep_id, file_id)),
    )
    monkeypatch.setattr(
        zenodo_client,
        'publish_deposition',
        lambda *a, **k: {
            'id': 'rec-2',
            'record_id': 'rec-2',
            'doi': '10.5281/zenodo.2',
        },
    )
    monkeypatch.setattr(
        zenodo_client, 'request_community_inclusion', lambda *a, **k: None
    )
    monkeypatch.setattr(
        zenodo_client, 'list_record_requests', lambda *a, **k: {'hits': {'hits': []}}
    )

    delete_zenodo_file._delete_published_file(dataset['id'], 'delete-me.txt')

    assert delete_calls == [('new-draft-1', 'file-2')]
    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.zenodo_deposition_id == 'new-draft-1'
    assert record.zenodo_record_id == 'rec-2'
    assert record.zenodo_record_doi == '10.5281/zenodo.2'
    # concept_record_id never changes across versions.
    assert record.concept_record_id == 'concept-1'
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['keep.txt']


def test_delete_published_file_resyncs_the_remaining_image_previews(monkeypatch):
    # A remaining image file gets a new per-version content link on
    # every republish (see media_preview.py's own docstring) - this
    # checks that the republish after a delete resyncs it, not just the
    # first publish (publish_guard.py's own job).
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.png')
    ZenodoDepositionQuery.add_file(dataset['id'], 'delete-me.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    record_url = publish_guard._zenodo_record_url('concept-1', True)
    photo_resource = factories.Resource(
        package_id=dataset['id'],
        name='photo.png',
        url=f'{record_url}?preview_file=photo.png',
        format='PNG',
    )

    monkeypatch.setattr(
        zenodo_client,
        'create_new_version',
        lambda *a, **k: {'id': 'new-draft-1', 'links': {'bucket': 'https://x/bucket'}},
    )
    monkeypatch.setattr(
        zenodo_client,
        'get_deposition',
        lambda *a, **k: {'files': [{'id': 'file-2', 'filename': 'delete-me.txt'}]},
    )
    monkeypatch.setattr(zenodo_client, 'delete_deposition_file', lambda *a, **k: None)
    monkeypatch.setattr(
        zenodo_client,
        'publish_deposition',
        lambda *a, **k: {
            'id': 'rec-2',
            'record_id': 'rec-2',
            'doi': '10.5281/zenodo.2',
            'files': [{'filename': 'photo.png'}],
        },
    )
    monkeypatch.setattr(
        zenodo_client, 'request_community_inclusion', lambda *a, **k: None
    )
    monkeypatch.setattr(
        zenodo_client, 'list_record_requests', lambda *a, **k: {'hits': {'hits': []}}
    )

    calls = []
    monkeypatch.setattr(
        delete_zenodo_file.media_preview,
        'sync_media_preview',
        lambda *a, **k: calls.append(a),
    )

    delete_zenodo_file._delete_published_file(dataset['id'], 'delete-me.txt')

    assert len(calls) == 1
    resource_id, fmt, content_url, user_name = calls[0]
    assert resource_id == photo_resource['id']
    assert fmt == 'PNG'
    assert content_url == (
        'https://sandbox.zenodo.org/api/records/rec-2/files/photo.png/content'
    )
    assert user_name


def test_delete_published_file_leaves_everything_unchanged_if_publish_fails(
    monkeypatch,
):
    # The known limitation this module's own docstring documents (e.g.
    # deleting the last remaining file - Zenodo rejects a 0-file
    # version) - must not partially update local state.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'only-file.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    monkeypatch.setattr(
        zenodo_client,
        'create_new_version',
        lambda *a, **k: {'id': 'new-draft-1', 'links': {'bucket': 'https://x/bucket'}},
    )
    monkeypatch.setattr(
        zenodo_client,
        'get_deposition',
        lambda *a, **k: {'files': [{'id': 'file-1', 'filename': 'only-file.txt'}]},
    )
    monkeypatch.setattr(zenodo_client, 'delete_deposition_file', lambda *a, **k: None)

    def failing_publish(*a, **k):
        raise zenodo_client.ZenodoError('Missing uploaded files')

    monkeypatch.setattr(zenodo_client, 'publish_deposition', failing_publish)

    delete_zenodo_file._delete_published_file(dataset['id'], 'only-file.txt')

    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.zenodo_deposition_id == 'dep-1'
    assert record.zenodo_record_id == 'rec-1'
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['only-file.txt']


def test_delete_published_file_on_an_unpublished_deposition_is_a_noop(monkeypatch):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')

    calls = []
    monkeypatch.setattr(
        zenodo_client, 'create_new_version', lambda *a, **k: calls.append(1)
    )

    delete_zenodo_file._delete_published_file(dataset['id'], 'keep.txt')

    assert calls == []


def test_delete_published_file_already_removed_is_a_noop(monkeypatch):
    # e.g. this job was retried after already succeeding on a previous
    # attempt.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'keep.txt')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    calls = []
    monkeypatch.setattr(
        zenodo_client, 'create_new_version', lambda *a, **k: calls.append(1)
    )

    delete_zenodo_file._delete_published_file(dataset['id'], 'already-gone.txt')

    assert calls == []
