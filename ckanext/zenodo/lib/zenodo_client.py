"""Minimal Zenodo REST client - just the handful of calls the upload
blueprint and the publish hook need: create a deposition, upload a file to
its bucket, update its metadata, publish it, and fetch it back.

Deliberately plain `requests` + module-level functions (no class, no
third-party Zenodo SDK, **no `ckan` import at all**), matching the house
style of ckanext-citations' openalex_client.py: every function takes the
access token (and, where it matters, whether to target the sandbox) as an
explicit argument rather than reading CKAN config itself. The caller (the
upload blueprint, the publish hook - both already need `ckan`) is
responsible for reading `ckanext.zenodo.*` config and passing the right
values in. This is what keeps this module importable and unit-testable
with no CKAN installation at all, in the lightweight CI image.

Retry with exponential backoff on 429/5xx and one dedicated error class,
same pattern as openalex_client.py/datacite_events_client.py.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

PRODUCTION_BASE_URL = 'https://zenodo.org/api'
SANDBOX_BASE_URL = 'https://sandbox.zenodo.org/api'

REQUEST_TIMEOUT = 20
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 1.0

# Zenodo's bucket PUT can legitimately take a long time for a large file -
# give it much more room than the quick JSON calls above.
UPLOAD_TIMEOUT = 600


class ZenodoError(Exception):
    """Raised when Zenodo can't answer a request (not "not found")."""


def base_url(sandbox):
    """Pure lookup, no config read - `sandbox` is whatever the caller
    decided (typically from ckanext.zenodo.test_mode)."""
    return SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL


def _request(method, url, access_token, *, timeout=REQUEST_TIMEOUT, **kwargs):
    """GET/POST/PUT with exponential backoff on 429 (rate limit) and
    5xx/network errors, and the bearer token attached. Returns the parsed
    JSON body, or None for a 404 (modelled as "not found", not an error)."""
    headers = dict(kwargs.pop('headers', None) or {})
    headers['Authorization'] = f'Bearer {access_token}'

    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, timeout=timeout, **kwargs
            )
        except requests.RequestException as e:
            last_error = f'Zenodo request failed: {e}'
            log.warning(
                '%s (attempt %s/%s, retrying in %.1fs)',
                last_error,
                attempt,
                MAX_RETRIES,
                backoff,
            )
            time.sleep(backoff)
            backoff *= 2.0
            continue

        if resp.status_code == 404:
            return None
        if resp.status_code in (200, 201, 202, 204):
            return resp.json() if resp.content else {}
        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = f'Zenodo returned {resp.status_code}: {resp.text[:200]}'
            log.warning(
                '%s (attempt %s/%s, retrying in %.1fs)',
                last_error,
                attempt,
                MAX_RETRIES,
                backoff,
            )
            time.sleep(backoff)
            backoff *= 2.0
            continue
        raise ZenodoError(f'Zenodo returned {resp.status_code}: {resp.text[:200]}')

    raise ZenodoError(
        f'Zenodo request failed after {MAX_RETRIES} attempts: {last_error}'
    )


def create_deposition(access_token, sandbox=False):
    """Create a new, empty deposition. Returns the full deposition dict
    (id, links.bucket, links.publish, ...)."""
    url = f'{base_url(sandbox)}/deposit/depositions'
    return _request('POST', url, access_token, json={'metadata': {}})


def get_deposition(deposition_id, access_token, sandbox=False):
    """Fetch a deposition's current state. Returns None if unknown."""
    url = f'{base_url(sandbox)}/deposit/depositions/{deposition_id}'
    return _request('GET', url, access_token)


def update_deposition_metadata(deposition_id, metadata, access_token, sandbox=False):
    """Replace a deposition's metadata (e.g. right before publishing)."""
    url = f'{base_url(sandbox)}/deposit/depositions/{deposition_id}'
    return _request('PUT', url, access_token, json={'metadata': metadata})


def upload_file_to_bucket(bucket_url, filename, file_obj, access_token):
    """Stream `file_obj` (any file-like/readable object) to the
    deposition's bucket under `filename`, via Zenodo's S3-like bucket API -
    not the legacy deposition/files multipart endpoint, which is
    discouraged for anything beyond small files. `bucket_url` already
    points at the right instance (sandbox or production), so there's no
    separate `sandbox` argument here. `requests` reads file-like objects
    incrementally itself; nothing here buffers the whole file into
    memory."""
    url = f'{bucket_url.rstrip("/")}/{filename}'
    return _request('PUT', url, access_token, data=file_obj, timeout=UPLOAD_TIMEOUT)


def publish_deposition(deposition_id, access_token, sandbox=False):
    """Publish a deposition - mints the DOI. Safe to call on an
    already-published deposition (Zenodo rejects it with a 4xx, which
    surfaces as a ZenodoError for the caller to catch and treat as a
    harmless no-op)."""
    url = f'{base_url(sandbox)}/deposit/depositions/{deposition_id}/actions/publish'
    return _request('POST', url, access_token)
