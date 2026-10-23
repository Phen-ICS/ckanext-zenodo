import configparser
import os
import re

import pytest

from ckanext.zenodo.lib import disk_guard, rate_limit, zenodo_client


def _resolve_ckan_ini(session):
    ckan_ini = getattr(session.config.option, 'ckan_ini', '') or ''
    if ckan_ini:
        return ckan_ini
    # pytest-ckan falls back to this env var when --ckan-ini is omitted -
    # mirror that so we inspect the config that will actually be used.
    return os.environ.get('CKAN_INI', '')


def _sqlalchemy_db_name(ini_path):
    """Best-effort extraction of the database name from an ini's
    sqlalchemy.url, without relying on CKAN's own config loader (which isn't
    initialised yet at pytest_sessionstart)."""
    if not ini_path or not os.path.isfile(ini_path):
        return None

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ini_path)
    except configparser.Error:
        return None

    url = None
    for section in parser.sections():
        if parser.has_option(section, 'sqlalchemy.url'):
            url = parser.get(section, 'sqlalchemy.url')
            break
    if not url:
        return None

    # test.ini/test-core.ini use PasteDeploy-style %(VAR)s placeholders
    # resolved against the environment (not plain configparser
    # interpolation) - expand them the same way so a templated URL still
    # resolves to the real database name.
    url = re.sub(r'%\((\w+)\)s', lambda m: os.environ.get(m.group(1), ''), url)
    url = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ''), url)
    return url.rsplit('/', 1)[-1].split('?', 1)[0]


def pytest_sessionstart(session):
    """Fail fast if tests would run against anything but an isolated test
    database.

    This check is NOT affected by ZENODO_ALLOW_NON_TEST_INI (below), which
    only relaxes the *path* check - never this one. A sibling extension
    (ckanext-citations) hit exactly this gap once: its equivalent bypass env
    var was set without --ckan-ini, so pytest-ckan silently fell back to the
    container's real CKAN_INI and a clean_db-using test dropped every table
    in the real local database.
    """
    ckan_ini = _resolve_ckan_ini(session)
    db_name = _sqlalchemy_db_name(ckan_ini)
    if db_name and not re.search('test', db_name, re.IGNORECASE):
        raise pytest.UsageError(
            f"Refusing to run: resolved database '{db_name}' (from "
            f'{ckan_ini or "CKAN_INI"}) does not look like a test database '
            "(expected a name containing 'test'). This check cannot be "
            'bypassed - point --ckan-ini (or CKAN_INI) at a test config.'
        )

    if os.environ.get('ZENODO_ALLOW_NON_TEST_INI') == '1':
        return

    normalized = ckan_ini.replace('\\', '/').strip()

    if normalized.endswith('/test.ini') or normalized == 'test.ini':
        return

    raise pytest.UsageError(
        'Unsafe CKAN test configuration detected. '
        'Run tests with --ckan-ini=/plugins/ckanext-zenodo/test.ini '
        '(or set ZENODO_ALLOW_NON_TEST_INI=1 to bypass intentionally).'
    )


@pytest.fixture
def zenodo_tables():
    """Create this plugin's tables in the test DB. The Alembic migration is
    what creates it in a real environment; tests only need the schema, and
    create_all(checkfirst=True) is idempotent."""
    from ckan.model import meta

    from ckanext.zenodo import model

    meta.metadata.create_all(
        bind=meta.engine,
        tables=[model.zenodo_deposition_table, model.zenodo_upload_table],
        checkfirst=True,
    )


# Fakes for the upload tests (test_blueprint_upload.py, test_chunked_upload.py):
# no real network, no real Redis state leaking between tests.


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


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rate_limit, 'connect_to_redis', lambda: fake)
    monkeypatch.setattr(disk_guard, 'connect_to_redis', lambda: fake)
    return fake


@pytest.fixture
def fake_disk_free(monkeypatch):
    """Deterministic, generous free space by default - independent of
    whatever's actually free on the machine running the tests. A test
    that cares about the disk-full path overrides this directly."""
    free = {'bytes': 20 * 1024**3}  # 20 GB
    monkeypatch.setattr(disk_guard, 'disk_free_bytes', lambda: free['bytes'])
    return free


@pytest.fixture
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
