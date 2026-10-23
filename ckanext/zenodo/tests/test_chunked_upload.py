"""Chunked upload (lib/chunked_upload.py): start, chunks, finish, the send
job, failures and the cleanup rules. Zenodo, Redis, the disk and the job
queue are faked (same fakes as test_blueprint_upload.py); the background
job runs right in the test."""

from datetime import datetime, timedelta, timezone

import pytest
from ckan.plugins import toolkit
from ckan.tests import factories

from ckanext.zenodo.lib import chunked_upload, disk_guard, zenodo_client
from ckanext.zenodo.model import ZenodoDepositionQuery, ZenodoUploadQuery
from ckanext.zenodo.model import zenodo_upload as upload_states

pytestmark = pytest.mark.usefixtures(
    'clean_db', 'zenodo_tables', 'fake_redis', 'fake_disk_free', 'fake_zenodo'
)


@pytest.fixture(autouse=True)
def upload_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(chunked_upload, 'upload_dir', lambda: str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def jobs(monkeypatch):
    """Queued jobs, run by the test itself."""
    queued = []
    monkeypatch.setattr(
        toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **_ignored: queued.append((fn, kwargs or {})),
    )
    return queued


def _run_send_jobs(jobs):
    for fn, kwargs in list(jobs):
        if fn is chunked_upload.send_to_zenodo:
            fn(**kwargs)


@pytest.fixture
def setup(app):
    user = factories.UserWithToken()
    org = factories.Organization(users=[{'name': user['name'], 'capacity': 'editor'}])
    dataset = factories.Dataset(owner_org=org['id'])
    client = app.flask_app.test_client()
    headers = {'Authorization': user['token']}
    return client, dataset, headers


def _start(client, dataset, headers, filename='scan.tif', size=10):
    return client.post(
        f'/zenodo/uploads/{dataset["name"]}',
        json={'filename': filename, 'size': size, 'force_zenodo': True},
        headers=headers,
    )


def _chunk(client, dataset, headers, upload_id, offset, data):
    return client.put(
        f'/zenodo/uploads/{dataset["name"]}/{upload_id}/chunk?offset={offset}',
        data=data,
        content_type='application/octet-stream',
        headers=headers,
    )


def _finish(client, dataset, headers, upload_id):
    return client.post(
        f'/zenodo/uploads/{dataset["name"]}/{upload_id}/finish', headers=headers
    )


def test_file_reaches_zenodo_after_chunks_and_the_send_job(
    setup, jobs, fake_zenodo, fake_redis
):
    client, dataset, headers = setup
    upload_id = _start(client, dataset, headers, size=10).json['result']['upload_id']
    assert fake_redis.store[disk_guard.RESERVED_KEY] == 10

    assert _chunk(client, dataset, headers, upload_id, 0, b'01234').status_code == 200
    # The answer was lost: the same chunk again replaces it.
    assert _chunk(client, dataset, headers, upload_id, 0, b'01234').status_code == 200
    assert _chunk(client, dataset, headers, upload_id, 5, b'56789').status_code == 200
    finish = _finish(client, dataset, headers, upload_id)
    assert finish.json['result']['status'] == upload_states.QUEUED
    assert fake_zenodo['uploaded'] == []

    _run_send_jobs(jobs)

    assert fake_zenodo['files'] == [{'filename': 'scan.tif', 'filesize': 10}]
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['scan.tif']
    assert ZenodoUploadQuery.get(upload_id) is None
    assert fake_redis.store[disk_guard.RESERVED_KEY] == 0


def test_chunks_are_checked(setup):
    client, dataset, headers = setup
    upload_id = _start(client, dataset, headers, size=10).json['result']['upload_id']

    # past what was received, past the announced size
    assert _chunk(client, dataset, headers, upload_id, 4, b'x').status_code == 416
    assert _chunk(client, dataset, headers, upload_id, 0, b'x' * 11).status_code == 416
    # finishing early
    assert _finish(client, dataset, headers, upload_id).status_code == 409

    other = factories.UserWithToken()
    assert _chunk(
        client, dataset, {'Authorization': other['token']}, upload_id, 0, b'x'
    ).status_code in (403, 404)


def test_start_applies_the_upload_rules(setup):
    client, dataset, headers = setup

    assert _start(client, dataset, headers, size=0).status_code == 400
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(disk_guard, 'disk_free_bytes', lambda: 5)
        assert _start(client, dataset, headers, size=10).status_code == 507


def test_failed_send_keeps_the_file_and_can_be_retried(
    setup, jobs, fake_zenodo, monkeypatch, upload_dir
):
    client, dataset, headers = setup
    upload_id = _start(client, dataset, headers, size=3).json['result']['upload_id']
    _chunk(client, dataset, headers, upload_id, 0, b'abc')
    _finish(client, dataset, headers, upload_id)

    def zenodo_down(*args, **kwargs):
        raise zenodo_client.ZenodoError('Zenodo returned 503')

    monkeypatch.setattr(zenodo_client, 'upload_file_to_bucket', zenodo_down)
    _run_send_jobs(jobs)

    upload = ZenodoUploadQuery.get(upload_id)
    assert upload.status == upload_states.FAILED
    assert upload.error == chunked_upload.SEND_FAILED
    assert (upload_dir / upload_id).read_bytes() == b'abc'

    jobs.clear()
    retry = client.post(
        f'/zenodo/uploads/{dataset["name"]}/{upload_id}/retry', headers=headers
    )
    assert retry.json['result']['status'] == upload_states.QUEUED
    assert len(jobs) == 1


def test_cancel_removes_the_file_and_the_reservation(setup, fake_redis, upload_dir):
    client, dataset, headers = setup
    upload_id = _start(client, dataset, headers, size=3).json['result']['upload_id']
    _chunk(client, dataset, headers, upload_id, 0, b'ab')

    response = client.delete(
        f'/zenodo/uploads/{dataset["name"]}/{upload_id}', headers=headers
    )

    assert response.status_code == 200
    assert ZenodoUploadQuery.get(upload_id) is None
    assert not (upload_dir / upload_id).exists()
    assert fake_redis.store[disk_guard.RESERVED_KEY] == 0


def test_housekeeping(setup, jobs):
    _client, dataset, _headers = setup
    user = factories.User()

    def make(status, idle, attempts=0):
        upload = ZenodoUploadQuery.create(dataset['id'], user['id'], 'f.bin', 1, True)
        ZenodoUploadQuery.update(upload, status=status, send_attempts=attempts)
        upload.updated = datetime.now(timezone.utc) - idle
        return upload.id

    abandoned = make(upload_states.RECEIVING, timedelta(hours=3))
    receiving = make(upload_states.RECEIVING, timedelta(minutes=5))
    stuck = make(upload_states.SENDING, timedelta(hours=7), attempts=1)
    retried = make(upload_states.FAILED, timedelta(hours=1), attempts=1)
    expired = make(upload_states.FAILED, timedelta(days=2), attempts=3)
    kept = make(upload_states.FAILED, timedelta(hours=1), attempts=3)

    counts = chunked_upload.housekeeping()

    assert counts == {'abandoned': 1, 'interrupted': 1, 'retried': 1, 'expired': 1}
    assert ZenodoUploadQuery.get(abandoned) is None
    assert ZenodoUploadQuery.get(expired) is None
    assert ZenodoUploadQuery.get(receiving).status == upload_states.RECEIVING
    assert ZenodoUploadQuery.get(stuck).status == upload_states.FAILED
    assert ZenodoUploadQuery.get(retried).status == upload_states.QUEUED
    assert ZenodoUploadQuery.get(kept).status == upload_states.FAILED
    assert [kwargs['upload_id'] for _fn, kwargs in jobs] == [retried]
