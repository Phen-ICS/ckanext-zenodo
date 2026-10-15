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


# Confirmed empirically during the Stage 8 sandbox smoke test: on the real
# (InvenioRDM-based) Zenodo, setting `metadata.communities` on a deposition
# does NOT associate the published record with that community by itself -
# it only creates a pending "community-inclusion" request against the
# record, which sits unaccepted (metadata.communities comes back None on
# the published record) until someone with curation rights on the
# community explicitly accepts it - even when that someone is the same
# shared account that owns the community. The two calls below close that
# gap; see publish_guard.py's accept_pending_community_inclusion for how
# they're used together right after a successful publish.


def list_record_requests(record_id, access_token, sandbox=False):
    """Lists the requests (inclusion, access, ...) open against a
    published record. Returns the raw search-result dict
    ({'hits': {'hits': [...]}, ...})."""
    url = f'{base_url(sandbox)}/records/{record_id}/requests'
    return _request('GET', url, access_token)


def accept_request(request_id, access_token, sandbox=False):
    """Accepts a pending request (e.g. a community-inclusion request) by
    id - the acting account must have curation rights on whatever the
    request targets (a community, here)."""
    url = f'{base_url(sandbox)}/requests/{request_id}/actions/accept'
    return _request('POST', url, access_token, json={})


def request_community_inclusion(
    record_id, community_identifier, access_token, sandbox=False
):
    """Explicitly submits a record for inclusion in a community via the
    dedicated endpoint, rather than relying on metadata.communities at
    publish time to trigger it as a side effect - confirmed empirically
    that the latter only does so for a brand new deposition's *first*
    publish; publishing a *new version* with metadata.communities
    merely copied over from the original (see create_new_version above)
    created no request at all, silently dropping the community
    association for every version after the first. Safe to call even
    when this exact record+community is already settled one way or
    another - Zenodo's own 400 for either "there's already an open
    request" (confirmed wording) or "the record is already included
    in this community" (confirmed separately, on a retry after an
    earlier call of this same function already got it accepted) is
    swallowed as a harmless no-op, same idea as publish_deposition's
    own already-published tolerance."""
    url = f'{base_url(sandbox)}/records/{record_id}/communities'
    try:
        return _request(
            'POST',
            url,
            access_token,
            json={'communities': [{'id': community_identifier}]},
        )
    except ZenodoError as e:
        if 'already an open inclusion request' in str(e) or (
            'already included in this community' in str(e)
        ):
            return None
        raise


# A published Zenodo record is immutable by design (DOIs are meant to be
# permanent) - there is no "delete a file from a published record"
# endpoint. Removing a file from an already-published deposition instead
# means: create a new draft version (a full copy of the latest version's
# files+metadata), delete the file from *that* draft, then publish it -
# the old version's own DOI stays frozen and resolvable with its
# original files forever, while the *concept* DOI (shared by every
# version, see metadata_mapper... no, see publish_guard.py's
# _zenodo_record_url, which already points resources at the concept
# record rather than any one version) starts resolving to this new
# version instead. Confirmed empirically against the real sandbox: v1
# published with 2 files, newversion -> draft copy with the same 2
# files, deleted 1, published as v2 (own new record id/DOI, 1 file) -
# v1's own record still shows both files afterwards, unchanged, and the
# concept URL that pointed at v1 now resolves to v2.
def create_new_version(deposition_id, access_token, sandbox=False):
    """Creates a new draft version of a published deposition (a copy of
    its latest version's files/metadata, itself unpublished) - returns
    the new draft's own deposition dict (its own 'id', 'links.bucket',
    etc., not the old one's)."""
    url = f'{base_url(sandbox)}/deposit/depositions/{deposition_id}/actions/newversion'
    return _request('POST', url, access_token)


def delete_deposition_file(deposition_id, file_id, access_token, sandbox=False):
    """Deletes one file from a deposition that is itself still a draft
    (either a never-yet-published deposition, or a new-version draft
    from create_new_version() above) - Zenodo rejects this on an
    already-published deposition. `file_id` is Zenodo's own internal id
    for that file (from get_deposition()'s `files[].id`), not its
    filename."""
    url = f'{base_url(sandbox)}/deposit/depositions/{deposition_id}/files/{file_id}'
    return _request('DELETE', url, access_token)
