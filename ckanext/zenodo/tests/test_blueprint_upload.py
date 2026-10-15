"""DB-backed tests for the /zenodo/upload/<package_name> blueprint -
needs a real CKAN app + DB, so --ckan-ini=/plugins/ckanext-zenodo/test.ini
is required (the conftest.py guard enforces this). zenodo_client and
Redis are both monkeypatched - no real network, no real Redis state
leaking between tests."""

import io

import pytest
from ckan.tests import factories

from ckanext.zenodo.blueprints import zenodo_upload as blueprint_module
from ckanext.zenodo.lib import rate_limit, zenodo_client
from ckanext.zenodo.model import ZenodoDepositionQuery

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


class FakeRedis:
    """Minimal in-memory stand-in for the handful of Redis calls
    rate_limit.py makes - avoids any real Redis state leaking between
    tests (and avoids depending on Redis being reachable at all here)."""

    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def incrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) + amount
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
    return fake


@pytest.fixture(autouse=True)
def fake_zenodo(monkeypatch):
    calls = {'created': [], 'uploaded': []}

    def fake_create_deposition(token, sandbox=False):
        deposition_id = f'dep-{len(calls["created"]) + 1}'
        calls['created'].append(deposition_id)
        return {
            'id': deposition_id,
            'links': {'bucket': f'https://sandbox.zenodo.org/bucket/{deposition_id}'},
        }

    def fake_upload_file_to_bucket(bucket_url, filename, file_obj, access_token):
        calls['uploaded'].append((bucket_url, filename))
        return {'filename': filename}

    monkeypatch.setattr(zenodo_client, 'create_deposition', fake_create_deposition)
    monkeypatch.setattr(
        zenodo_client, 'upload_file_to_bucket', fake_upload_file_to_bucket
    )
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
