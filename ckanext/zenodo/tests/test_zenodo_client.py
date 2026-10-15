"""Pure unit tests - no ckan import, no DB, no real network. zenodo_client
is monkeypatched at the `requests.request` boundary (its only real
dependency), matching ckanext-citations' convention of mocking at the
lowest layer a module actually calls rather than introducing a separate
HTTP-mocking library."""

import pytest

from ckanext.zenodo.lib import zenodo_client


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=''):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.content = b'1' if json_body is not None else b''

    def json(self):
        return self._json_body


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    # Retry backoff would otherwise really sleep (1s, 2s, 4s, ...) in tests.
    monkeypatch.setattr(zenodo_client.time, 'sleep', lambda seconds: None)


def test_base_url_sandbox_vs_production():
    assert zenodo_client.base_url(True) == zenodo_client.SANDBOX_BASE_URL
    assert zenodo_client.base_url(False) == zenodo_client.PRODUCTION_BASE_URL


def test_request_returns_parsed_json_on_success(monkeypatch):
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, headers, kwargs))
        return FakeResponse(200, {'id': 123})

    monkeypatch.setattr(zenodo_client.requests, 'request', fake_request)

    result = zenodo_client._request('GET', 'https://example.test/x', 'tok123')

    assert result == {'id': 123}
    _method, _url, headers, _kwargs = calls[0]
    assert headers['Authorization'] == 'Bearer tok123'


def test_request_404_returns_none(monkeypatch):
    monkeypatch.setattr(
        zenodo_client.requests, 'request', lambda *a, **k: FakeResponse(404)
    )

    assert zenodo_client._request('GET', 'https://example.test/x', 'tok') is None


def test_request_retries_on_429_then_succeeds(monkeypatch):
    responses = [FakeResponse(429, text='slow down'), FakeResponse(200, {'ok': True})]

    def fake_request(*a, **k):
        return responses.pop(0)

    monkeypatch.setattr(zenodo_client.requests, 'request', fake_request)

    assert zenodo_client._request('GET', 'https://example.test/x', 'tok') == {
        'ok': True
    }


def test_request_retries_on_5xx_then_succeeds(monkeypatch):
    responses = [FakeResponse(503, text='down'), FakeResponse(200, {'ok': True})]
    monkeypatch.setattr(
        zenodo_client.requests, 'request', lambda *a, **k: responses.pop(0)
    )

    assert zenodo_client._request('GET', 'https://example.test/x', 'tok') == {
        'ok': True
    }


def test_request_exhausts_retries_and_raises(monkeypatch):
    monkeypatch.setattr(
        zenodo_client.requests,
        'request',
        lambda *a, **k: FakeResponse(500, text='still down'),
    )

    with pytest.raises(zenodo_client.ZenodoError):
        zenodo_client._request('GET', 'https://example.test/x', 'tok')


def test_request_retries_on_network_exception_then_succeeds(monkeypatch):
    import requests as requests_module

    attempts = {'n': 0}

    def fake_request(*a, **k):
        attempts['n'] += 1
        if attempts['n'] == 1:
            raise requests_module.ConnectionError('boom')
        return FakeResponse(200, {'ok': True})

    monkeypatch.setattr(zenodo_client.requests, 'request', fake_request)

    assert zenodo_client._request('GET', 'https://example.test/x', 'tok') == {
        'ok': True
    }


def test_request_non_retryable_status_raises_immediately(monkeypatch):
    calls = {'n': 0}

    def fake_request(*a, **k):
        calls['n'] += 1
        return FakeResponse(403, text='forbidden')

    monkeypatch.setattr(zenodo_client.requests, 'request', fake_request)

    with pytest.raises(zenodo_client.ZenodoError):
        zenodo_client._request('GET', 'https://example.test/x', 'tok')
    assert calls['n'] == 1  # no retry on a non-429/5xx error


def test_create_deposition_posts_to_depositions_endpoint(monkeypatch):
    captured = {}

    def fake_request(method, url, access_token, **kwargs):
        captured.update(method=method, url=url, access_token=access_token, **kwargs)
        return {'id': 42, 'links': {'bucket': 'https://example.test/bucket'}}

    monkeypatch.setattr(zenodo_client, '_request', fake_request)

    result = zenodo_client.create_deposition('tok', sandbox=True)

    assert result['id'] == 42
    assert captured['method'] == 'POST'
    assert captured['url'] == f'{zenodo_client.SANDBOX_BASE_URL}/deposit/depositions'
    assert captured['access_token'] == 'tok'
    assert captured['json'] == {'metadata': {}}


def test_get_deposition_builds_correct_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: (
            captured.update(method=method, url=url) or {'id': 99}
        ),
    )

    zenodo_client.get_deposition(99, 'tok', sandbox=False)

    assert captured['method'] == 'GET'
    assert (
        captured['url'] == f'{zenodo_client.PRODUCTION_BASE_URL}/deposit/depositions/99'
    )


def test_update_deposition_metadata_sends_metadata_body(monkeypatch):
    captured = {}

    def fake_request(method, url, token, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return {'id': 1}

    monkeypatch.setattr(zenodo_client, '_request', fake_request)

    metadata = {'title': 'A dataset'}
    zenodo_client.update_deposition_metadata(1, metadata, 'tok', sandbox=True)

    assert captured['method'] == 'PUT'
    assert captured['json'] == {'metadata': metadata}


def test_publish_deposition_hits_publish_action(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: (
            captured.update(method=method, url=url)
            or {'id': 1, 'doi': '10.5281/zenodo.1'}
        ),
    )

    result = zenodo_client.publish_deposition(1, 'tok', sandbox=True)

    assert result['doi'] == '10.5281/zenodo.1'
    assert captured['url'].endswith('/deposit/depositions/1/actions/publish')


def test_upload_file_to_bucket_strips_trailing_slash(monkeypatch):
    captured = {}

    def fake_request(method, url, token, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return {'filename': 'data.csv'}

    monkeypatch.setattr(zenodo_client, '_request', fake_request)

    fake_file = object()
    zenodo_client.upload_file_to_bucket(
        'https://example.test/bucket/', 'data.csv', fake_file, 'tok'
    )

    assert captured['url'] == 'https://example.test/bucket/data.csv'
    assert captured['data'] is fake_file
    assert captured['timeout'] == zenodo_client.UPLOAD_TIMEOUT


def test_list_record_requests_hits_records_requests_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: (
            captured.update(method=method, url=url) or {'hits': {'hits': []}}
        ),
    )

    result = zenodo_client.list_record_requests(42, 'tok', sandbox=True)

    assert captured['method'] == 'GET'
    assert captured['url'].endswith('/records/42/requests')
    assert result == {'hits': {'hits': []}}


def test_accept_request_hits_accept_action(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: (
            captured.update(method=method, url=url, **k) or {'status': 'accepted'}
        ),
    )

    result = zenodo_client.accept_request('req-id', 'tok', sandbox=True)

    assert captured['method'] == 'POST'
    assert captured['url'].endswith('/requests/req-id/actions/accept')
    assert captured['json'] == {}
    assert result == {'status': 'accepted'}


def test_create_new_version_hits_newversion_action(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: (
            captured.update(method=method, url=url) or {'id': 'new-draft-1'}
        ),
    )

    result = zenodo_client.create_new_version(42, 'tok', sandbox=True)

    assert captured['method'] == 'POST'
    assert captured['url'].endswith('/deposit/depositions/42/actions/newversion')
    assert result == {'id': 'new-draft-1'}


def test_delete_deposition_file_hits_the_files_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: captured.update(method=method, url=url) or {},
    )

    zenodo_client.delete_deposition_file(42, 'file-id-1', 'tok', sandbox=True)

    assert captured['method'] == 'DELETE'
    assert captured['url'].endswith('/deposit/depositions/42/files/file-id-1')


def test_request_community_inclusion_hits_the_communities_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        zenodo_client,
        '_request',
        lambda method, url, token, **k: (
            captured.update(method=method, url=url, **k) or {}
        ),
    )

    zenodo_client.request_community_inclusion(42, 'fair3r', 'tok', sandbox=True)

    assert captured['method'] == 'POST'
    assert captured['url'].endswith('/records/42/communities')
    assert captured['json'] == {'communities': [{'id': 'fair3r'}]}


def test_request_community_inclusion_swallows_an_already_open_request(monkeypatch):
    def already_open(*a, **k):
        raise zenodo_client.ZenodoError(
            'Zenodo returned 400: {"errors": [{"message": '
            '"There is already an open inclusion request for this community."}]}'
        )

    monkeypatch.setattr(zenodo_client, '_request', already_open)

    # Must not raise.
    result = zenodo_client.request_community_inclusion(
        42, 'fair3r', 'tok', sandbox=True
    )

    assert result is None


def test_request_community_inclusion_swallows_an_already_included_record(monkeypatch):
    # Confirmed empirically against the real sandbox: a retry after an
    # earlier call of this same function already got the request
    # accepted gets this different wording, not the "already open
    # request" one.
    def already_included(*a, **k):
        raise zenodo_client.ZenodoError(
            'Zenodo returned 400: {"errors":[{"community_id":"fair3r",'
            '"message":"The record is already included in this community."}]}'
        )

    monkeypatch.setattr(zenodo_client, '_request', already_included)

    result = zenodo_client.request_community_inclusion(
        42, 'fair3r', 'tok', sandbox=True
    )

    assert result is None


def test_request_community_inclusion_reraises_other_errors(monkeypatch):
    def other_error(*a, **k):
        raise zenodo_client.ZenodoError('Zenodo returned 500: boom')

    monkeypatch.setattr(zenodo_client, '_request', other_error)

    with pytest.raises(zenodo_client.ZenodoError):
        zenodo_client.request_community_inclusion(42, 'fair3r', 'tok', sandbox=True)
