"""DB-backed tests for the /zenodo/upload/<package_name> blueprint -
needs a real CKAN app + DB, so --ckan-ini=/plugins/ckanext-zenodo/test.ini
is required (the conftest.py guard enforces this). zenodo_client and
Redis are both monkeypatched - no real network, no real Redis state
leaking between tests."""

import io

import pytest
from ckan.tests import factories

from ckanext.zenodo.blueprints import zenodo_upload as blueprint_module
from ckanext.zenodo.lib import disk_guard, publish_guard, rate_limit, zenodo_client
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


class FakeRedis:
    """Minimal in-memory stand-in for the handful of Redis calls
    rate_limit.py and disk_guard.py make - avoids any real Redis state
    leaking between tests (and avoids depending on Redis being reachable
    at all here). Shared between both modules (same store) so a test
    that exercises the real upload() view sees one consistent counter
    set, same as in production where both read the same Redis."""

    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def incrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    def decrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) - amount
        return self.store[key]

    def expire(self, key, seconds):
        pass

    def get(self, key):
        value = self.store.get(key)
        return str(value).encode() if value is not None else None


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rate_limit, 'connect_to_redis', lambda: fake)
    monkeypatch.setattr(disk_guard, 'connect_to_redis', lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def fake_disk_free(monkeypatch):
    """Deterministic, generous free space by default - independent of
    whatever's actually free on the machine running the tests. A test
    that cares about the disk-full path overrides this directly."""
    free = {'bytes': 20 * 1024**3}  # 20 GB
    monkeypatch.setattr(disk_guard, 'disk_free_bytes', lambda: free['bytes'])
    return free


@pytest.fixture(autouse=True)
def fake_zenodo(monkeypatch):
    calls = {'created': [], 'uploaded': [], 'files': []}

    def fake_create_deposition(token, sandbox=False):
        deposition_id = f'dep-{len(calls["created"]) + 1}'
        calls['created'].append(deposition_id)
        return {
            'id': deposition_id,
            'links': {'bucket': f'https://sandbox.zenodo.org/bucket/{deposition_id}'},
        }

    def fake_upload_file_to_bucket(bucket_url, filename, file_obj, access_token):
        calls['uploaded'].append((bucket_url, filename))
        calls['files'].append({'filename': filename, 'filesize': len(file_obj.read())})
        return {'filename': filename}

    def fake_get_deposition(deposition_id, access_token, sandbox=False):
        return {'files': list(calls['files'])}

    monkeypatch.setattr(zenodo_client, 'create_deposition', fake_create_deposition)
    monkeypatch.setattr(
        zenodo_client, 'upload_file_to_bucket', fake_upload_file_to_bucket
    )
    monkeypatch.setattr(zenodo_client, 'get_deposition', fake_get_deposition)
    return calls


def _upload(app, package_name, filename, content, token=None, force=False):
    headers = {'Authorization': token} if token else {}
    data = {'upload': (io.BytesIO(content), filename)}
    if force:
        data['force_zenodo'] = 'true'
    return app.post(
        f'/zenodo/upload/{package_name}',
        data=data,
        headers=headers,
        content_type='multipart/form-data',
    )


def test_upload_requires_login(app):
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _upload(app, dataset['name'], 'archive.zip', b'x' * 10)

    assert response.status_code == 403


@pytest.mark.ckan_config('ckanext.xloader.formats', 'csv application/csv')
@pytest.mark.ckan_config('ckan.max_resource_size', '1')
def test_small_readable_file_is_rejected_without_force(app):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _upload(
        app, dataset['name'], 'results.csv', b'a,b\n1,2\n', token=user['token']
    )

    assert response.status_code == 400
    assert 'force Zenodo' in response.json['error']['message']


@pytest.mark.ckan_config('ckanext.xloader.formats', 'csv application/csv')
@pytest.mark.ckan_config('ckan.max_resource_size', '1')
def test_small_readable_file_is_accepted_when_forced(app, fake_zenodo):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _upload(
        app,
        dataset['name'],
        'results.csv',
        b'a,b\n1,2\n',
        token=user['token'],
        force=True,
    )

    assert response.status_code == 200
    assert response.json['success'] is True
    assert len(fake_zenodo['uploaded']) == 1


@pytest.mark.ckan_config('ckan.max_resource_size', '1')
def test_oversized_file_is_accepted_without_force(app, fake_zenodo):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    big_content = b'x' * (2 * 1024 * 1024)  # 2 MB > the 1 MB ckan.max_resource_size
    response = _upload(
        app, dataset['name'], 'huge.csv', big_content, token=user['token']
    )

    assert response.status_code == 200
    assert len(fake_zenodo['uploaded']) == 1


@pytest.mark.ckan_config('ckanext.xloader.formats', 'csv application/csv')
def test_small_unreadable_format_is_accepted_without_force(app, fake_zenodo):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _upload(
        app, dataset['name'], 'archive.zip', b'not a real zip', token=user['token']
    )

    assert response.status_code == 200
    assert len(fake_zenodo['uploaded']) == 1


def test_empty_file_is_rejected(app):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _upload(app, dataset['name'], 'empty.zip', b'', token=user['token'])

    assert response.status_code == 400


def test_second_upload_reuses_existing_open_deposition(app, fake_zenodo):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])
    _upload(app, dataset['name'], 'b.zip', b'y' * 10, token=user['token'])

    assert len(fake_zenodo['created']) == 1, (
        'the second upload should reuse the first upload own open deposition, '
        'not create a second one'
    )
    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.file_count == 2
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['a.zip', 'b.zip']


def test_upload_to_an_already_published_dataset_creates_a_new_version(
    app, fake_zenodo, monkeypatch
):
    # Found by real testing: uploading straight into an already-
    # published deposition's own bucket silently "succeeds" but the
    # file never appears on the public record at all. The fix routes
    # it through Zenodo's own new-version-and-republish dance instead -
    # see zenodo_upload._bucket_for_upload's own docstring.
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])
    ZenodoDepositionQuery.create(
        dataset['id'], 'dep-1', 'https://example.test/bucket-1'
    )
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    monkeypatch.setattr(
        zenodo_client,
        'create_new_version',
        lambda *a, **k: {
            'id': 'dep-2',
            'links': {'bucket': 'https://example.test/bucket-2'},
        },
    )
    enqueued = []
    monkeypatch.setattr(
        publish_guard.toolkit,
        'enqueue_job',
        lambda fn, kwargs=None, **kw: enqueued.append((fn, kwargs)),
    )

    response = _upload(
        app, dataset['name'], 'results.csv', b'x' * 10, token=user['token'], force=True
    )

    assert response.status_code == 200
    assert fake_zenodo['uploaded'] == [('https://example.test/bucket-2', 'results.csv')]

    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.zenodo_deposition_id == 'dep-2'
    assert record.bucket_url == 'https://example.test/bucket-2'
    assert record.published is False
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == [
        'photo.jpg',
        'results.csv',
    ]
    # The dataset is already active + public - this new draft needs its
    # own republish to actually surface the file, which must be
    # enqueued (never run synchronously from this request).
    assert len(enqueued) == 1
    assert enqueued[0][1] == {'package_id': dataset['id']}


def test_upload_to_an_already_published_dataset_is_untouched_if_the_upload_fails(
    app, monkeypatch
):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])
    ZenodoDepositionQuery.create(
        dataset['id'], 'dep-1', 'https://example.test/bucket-1'
    )
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    monkeypatch.setattr(
        zenodo_client,
        'create_new_version',
        lambda *a, **k: {
            'id': 'dep-2',
            'links': {'bucket': 'https://example.test/bucket-2'},
        },
    )

    def failing_upload(*a, **k):
        raise zenodo_client.ZenodoError('boom')

    monkeypatch.setattr(zenodo_client, 'upload_file_to_bucket', failing_upload)

    response = _upload(
        app, dataset['name'], 'results.csv', b'x' * 10, token=user['token'], force=True
    )

    assert response.status_code == 502
    # Still pointing at the original, still-valid published deposition -
    # not left dangling on the abandoned new-version draft.
    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.zenodo_deposition_id == 'dep-1'
    assert record.bucket_url == 'https://example.test/bucket-1'
    assert record.published is True
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['photo.jpg']


def test_upload_to_an_already_published_dataset_is_untouched_if_new_version_fails(
    app, monkeypatch
):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])
    ZenodoDepositionQuery.create(
        dataset['id'], 'dep-1', 'https://example.test/bucket-1'
    )
    ZenodoDepositionQuery.add_file(dataset['id'], 'photo.jpg')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    def failing_new_version(*a, **k):
        raise zenodo_client.ZenodoError('boom')

    monkeypatch.setattr(zenodo_client, 'create_new_version', failing_new_version)

    response = _upload(
        app, dataset['name'], 'results.csv', b'x' * 10, token=user['token'], force=True
    )

    assert response.status_code == 502
    record = ZenodoDepositionQuery.read_package(dataset['id'])
    assert record.zenodo_deposition_id == 'dep-1'
    assert record.published is True
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == ['photo.jpg']


@pytest.mark.ckan_config('ckanext.zenodo.max_uploads_per_hour', '2')
def test_request_rate_limit_is_enforced(app):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    for _ in range(2):
        _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])

    response = _upload(app, dataset['name'], 'c.zip', b'x' * 10, token=user['token'])

    assert response.status_code == 429


@pytest.mark.ckan_config('ckanext.zenodo.max_bytes_per_hour', '15')
def test_byte_volume_rate_limit_is_enforced(app):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])
    response = _upload(app, dataset['name'], 'b.zip', b'x' * 10, token=user['token'])

    assert response.status_code == 429


def test_upload_rejected_when_not_enough_disk_space(app, fake_disk_free):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    fake_disk_free['bytes'] = 1024  # 1 KB free - nothing fits

    response = _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])

    assert response.status_code == 507


def test_disk_space_is_released_after_a_rejected_upload(app, fake_disk_free):
    """A file too big/wrong-format for CKAN storage but still rejected
    for another reason (e.g. by the force-Zenodo gate) must not leave
    its disk reservation stuck - the next upload needs that room back."""
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    _upload(app, dataset['name'], 'empty.zip', b'', token=user['token'])

    assert disk_guard.reserved_bytes() == 0


def test_upload_capacity_requires_login(app):
    response = app.get('/zenodo/upload-capacity')

    assert response.status_code == 403


def test_upload_capacity_reflects_margin_and_reservations(app, fake_disk_free):
    user = factories.SysadminWithToken()
    fake_disk_free['bytes'] = 20 * 1024**3

    response = app.get(
        '/zenodo/upload-capacity', headers={'Authorization': user['token']}
    )

    assert response.status_code == 200
    # 20 GB free - 5 GB default margin = 15 GB of disk headroom, but
    # capped by the default 10 GB max_file_size_bytes - a bigger file
    # would be rejected by upload() itself regardless of disk space.
    assert response.json['result']['available_bytes'] == 10 * 1024**3


@pytest.mark.ckan_config('ckanext.zenodo.max_file_size_bytes', str(2 * 1024**3))
def test_upload_capacity_is_not_capped_below_a_tighter_max_file_size(
    app, fake_disk_free
):
    user = factories.SysadminWithToken()
    fake_disk_free['bytes'] = 20 * 1024**3

    response = app.get(
        '/zenodo/upload-capacity', headers={'Authorization': user['token']}
    )

    # 20 GB free - 5 GB margin = 15 GB of disk headroom, but max_file_size_bytes
    # is configured tighter here (2 GB) - that's the binding constraint.
    assert response.json['result']['available_bytes'] == 2 * 1024**3


def test_upload_capacity_reflects_disk_pressure_below_max_file_size(
    app, fake_disk_free
):
    user = factories.SysadminWithToken()
    fake_disk_free['bytes'] = 6 * 1024**3  # less than the margin + default cap

    response = app.get(
        '/zenodo/upload-capacity', headers={'Authorization': user['token']}
    )

    # 6 GB free - 5 GB margin = 1 GB - disk is the binding constraint here,
    # well below the default 10 GB max_file_size_bytes.
    assert response.json['result']['available_bytes'] == 1 * 1024**3


@pytest.mark.ckan_config('ckanext.zenodo.max_files_per_record', '2')
def test_upload_rejected_when_record_file_count_limit_reached(app, fake_zenodo):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    for name in ('a.zip', 'b.zip'):
        assert (
            _upload(
                app, dataset['name'], name, b'x' * 10, token=user['token']
            ).status_code
            == 200
        )

    response = _upload(app, dataset['name'], 'c.zip', b'x' * 10, token=user['token'])

    assert response.status_code == 409
    assert 'at most 2 files' in response.json['error']['message']
    assert len(fake_zenodo['uploaded']) == 2


@pytest.mark.ckan_config('ckanext.zenodo.max_record_bytes', '2000')
def test_upload_rejected_when_record_total_size_would_be_exceeded(app, fake_zenodo):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    first = _upload(app, dataset['name'], 'a.zip', b'x' * 1000, token=user['token'])
    second = _upload(app, dataset['name'], 'b.zip', b'x' * 1000, token=user['token'])

    assert first.status_code == 200
    assert second.status_code == 409
    assert 'in total per record' in second.json['error']['message']
    assert len(fake_zenodo['uploaded']) == 1


@pytest.mark.ckan_config('ckanext.zenodo.max_files_per_record', '0')
def test_record_limit_rejection_leaves_no_disk_reservation(app):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])

    assert response.status_code == 409
    assert disk_guard.reserved_bytes() == 0


def test_zenodo_error_returns_502(app, monkeypatch):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    def failing_create_deposition(token, sandbox=False):
        raise zenodo_client.ZenodoError('boom')

    monkeypatch.setattr(
        blueprint_module.zenodo_client, 'create_deposition', failing_create_deposition
    )

    response = _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])

    assert response.status_code == 502


def _delete(app, package_name, filename, token=None):
    # CKANTestApp only wraps get/post/options itself - DELETE goes
    # through its own underlying werkzeug test client (app.client)
    # directly instead.
    headers = {'Authorization': token} if token else {}
    return app.client.delete(
        f'/zenodo/upload/{package_name}/{filename}', headers=headers
    )


def test_delete_pending_requires_login(app):
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])

    response = _delete(app, dataset['name'], 'a.zip')

    assert response.status_code == 403


def test_delete_pending_removes_an_uploaded_file(app, fake_zenodo, monkeypatch):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])
    _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])

    monkeypatch.setattr(
        zenodo_client,
        'get_deposition',
        lambda *a, **k: {'files': [{'id': 'file-1', 'filename': 'a.zip'}]},
    )
    deleted = []
    monkeypatch.setattr(
        zenodo_client,
        'delete_deposition_file',
        lambda dep_id, file_id, *a, **k: deleted.append((dep_id, file_id)),
    )

    response = _delete(app, dataset['name'], 'a.zip', token=user['token'])

    assert response.status_code == 200
    assert response.json['success'] is True
    assert deleted == [('dep-1', 'file-1')]
    assert ZenodoDepositionQuery.list_filenames(dataset['id']) == []


def test_delete_pending_unknown_filename_returns_404(app):
    user = factories.SysadminWithToken()
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'])
    _upload(app, dataset['name'], 'a.zip', b'x' * 10, token=user['token'])

    response = _delete(app, dataset['name'], 'nope.zip', token=user['token'])

    assert response.status_code == 404
