"""Template helper - CKAN-only on purpose (reads the model via
ZenodoDepositionQuery), unlike the rest of lib/.

Only covers the transient gap between a file reaching Zenodo and the
real CKAN resource existing for it (see publish_guard.py's
_create_zenodo_resources): the upload itself is synchronous, but
turning it into a published Zenodo record + CKAN Link resources is a
deferred RQ job, only enqueued once the dataset is active AND public
(see maybe_publish_pending_deposition). Someone landing on the dataset
page in that window would otherwise see CKAN's own plain "This dataset
has no data" right after successfully uploading a file - this says
"hang on" instead, with two genuinely different reasons:

- the dataset is still private - the job won't even be enqueued until
  it's made public (reloading changes nothing here, so saying so would
  be actively misleading).
- the dataset is already public - the job is queued/running and should
  finish shortly, so "reload in a moment" is actually accurate here.

Once the job runs, real resources exist and this helper stops returning
anything (the dataset no longer has a pending, unpublished deposition),
so there is nothing left to show here and no risk of it ever
duplicating the real resources.

Confirmed empirically: CKAN renders the dataset page's own template at
least twice for a single incoming request (not something this plugin
controls or fully understands the reason for - a cache-key/ETag
computation pass is one plausible explanation, not confirmed). A
destructive read like zenodo_notice_for_package's own pop_notice()
would otherwise have its result consumed by whichever render happens
first and discarded, only for the *next* (real, final) render to find
nothing left - exactly what was observed before this got memoized
per-request via flask.g below.

A second, separate issue was found (and fixed, in
ZenodoDepositionQuery.pop_notice itself) while verifying the above: a
plain dataset-page GET never goes through CKAN's own action-layer
commit_and_remove(), unlike resource_create/package_update. Without an
explicit commit there, pop_notice's clearing write was only ever
visible within the one request that made it (same open transaction,
so it still looked cleared *to that request*) and got silently rolled
back once the request ended - so the very next, separate pageview
re-read the same still-set notice from the DB and showed it again,
forever, instead of once. Confirmed empirically via direct psql checks
across several real HTTP requests before this was fixed.
"""

import json
import re

from ckan import plugins
from ckan.plugins import toolkit
from flask import g, has_app_context

from ckanext.zenodo.lib import zenodo_config
from ckanext.zenodo.lib.chunked_upload import MAX_SEND_ATTEMPTS
from ckanext.zenodo.model import ZenodoDepositionQuery, ZenodoUploadQuery

# Keyed by the short code ZenodoDepositionQuery.set_notice() stores (see
# publish_guard.py/delete_zenodo_file.py's own call sites) - a plain string
# column, not a DB enum, so a new notice kind only needs an entry here, no
# migration.


def N_(message):
    """Marks a string for translation extraction without translating it
    yet - these are translated at render time below, in the viewer's own
    language, not once at import (which would freeze one language for
    everyone)."""
    return message


_NOTICE_MESSAGES = {
    'published': N_('Archived on Zenodo'),
    'new_version_published': N_('Zenodo archive updated'),
}


def zenodo_notice_for_package(pkg_dict):
    """A one-shot "flash message, but surviving past this request" -
    see ZenodoDepositionQuery.set_notice's own docstring for why a real
    flash_success() (session-based, same request only - see
    ckanext-doi's own synchronous equivalent) can't be used for
    something a deferred RQ job wants to announce. Returns None (the
    common case) if there's nothing to show; otherwise a dict
    {'message': ...} and clears it, so it only ever shows once.

    Memoized per-request via flask.g (when there's an app context to
    hang it on - a plain unit test calling this directly, with no
    request/app context at all, just skips memoization and reads
    straight from the DB every time, which is exactly what the
    existing tests for this function already assume) - see this
    module's own docstring for why a plain destructive read breaks
    under CKAN's own multiple-renders-per-request behavior: every call
    within the same request (however many that turns out to be)
    returns the one result the *first* call actually popped, instead
    of each one popping (and emptying) it in turn."""
    package_id = pkg_dict.get('id')
    if not package_id:
        return None

    cache_attr = f'_zenodo_notice_{package_id}'
    memoize = has_app_context()
    if memoize and hasattr(g, cache_attr):
        return getattr(g, cache_attr)

    notice = ZenodoDepositionQuery.pop_notice(package_id)
    result = None
    if notice is not None:
        message = toolkit._(_NOTICE_MESSAGES.get(notice, notice))
        deposition = ZenodoDepositionQuery.read_package(package_id)
        if deposition and deposition.zenodo_record_doi:
            message = f'{message} (DOI: {deposition.zenodo_record_doi})'
        result = {'message': f'{message}.'}

    if memoize:
        setattr(g, cache_attr, result)
    return result


def zenodo_pending_deposition_for_package(pkg_dict):
    """Returns None if there's nothing pending (no deposition, no files
    yet, or already published - a real resource exists for it by then).
    Otherwise a dict {'waiting_for_public': bool, 'filenames': [...]} -
    the template picks the right message from the former, and lists the
    latter (the real files already on Zenodo, read back from the local
    row rather than a live Zenodo API call on every page render - see
    ZenodoDepositionQuery.add_file, called by the upload blueprint).

    Excludes any filename that already has a real CKAN resource: a
    deposition can go published -> unpublished again (zenodo_upload.py's
    chunked_upload.bucket_for_upload, for a file added after the dataset's first
    publish - see its own docstring) while some of its files already
    have one from that earlier publish. Without this filter, every one
    of them would show here *again*, duplicating what's already in the
    normal resource list just below - this only ever lists the file(s)
    that genuinely don't have one yet."""
    package_id = pkg_dict.get('id')
    if not package_id:
        return None

    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.published:
        return None
    if deposition.file_count <= 0:
        return None

    existing_resource_names = {r.get('name') for r in pkg_dict.get('resources') or []}
    filenames = [
        f
        for f in ZenodoDepositionQuery.list_filenames(package_id)
        if f not in existing_resource_names
    ]
    if not filenames:
        return None

    return {
        'waiting_for_public': bool(pkg_dict.get('private')),
        # retry-pending has given up on it (see cli.py) - "reload in a
        # moment" would no longer be true.
        'publish_failed': (deposition.publish_attempts or 0)
        >= zenodo_config.max_publish_attempts(),
        'filenames': filenames,
    }


def zenodo_delete_impact(pkg_dict):
    """For the dataset delete confirmation: 'draft' if deleting it will
    also delete its not-yet-published files on Zenodo (irreversible -
    see lifecycle_guard.py), 'published' if its files stay public on
    Zenodo regardless, None if it has nothing there."""
    deposition = ZenodoDepositionQuery.read_package(pkg_dict.get('id'))
    if deposition is None:
        return None
    if deposition.concept_record_id:
        return 'published'
    return 'draft' if deposition.file_count > 0 else None


_ZENODO_DESCRIPTION = re.compile(
    r'^Zenodo · DOI \[([^\]]+)\]\((https://[^)\s]+)\)(.*)$'
)


def zenodo_description_parts(description):
    """Splits a Zenodo resource's description (see publish_guard.py's
    _create_zenodo_resources: "Zenodo · DOI [<doi>](<url>) · sandbox")
    into {'doi', 'url', 'rest'} for the template to build the link itself
    - with target=_blank, which Markdown can't express - Jinja-escaped,
    no Markup. Only an https:// URL matches: the description is editable
    by the user, so anything else (e.g. a javascript: link typed into it)
    falls back to CKAN's own sanitized Markdown rendering (None here)."""
    match = _ZENODO_DESCRIPTION.match(description or '')
    if not match:
        return None
    doi, url, rest = match.groups()
    return {'doi': doi, 'url': url, 'rest': rest}


def zenodo_standalone_upload_config():
    """The routing config (JSON) for this plugin's own, built-in upload form,
    or None when that form must not appear: Zenodo isn't configured, or
    ckanext-fair3r is enabled and already drives the resource form with its
    own, richer script (two scripts must never intercept the same submit).
    """
    if plugins.plugin_loaded('fair3r'):
        return None
    config = zenodo_config.routing_config_dict()
    return json.dumps(config) if config else None


def zenodo_uploads_for_package(pkg_dict):
    """Chunked uploads not yet on Zenodo (lib/chunked_upload.py), for the
    dataset's editors: being received, waiting for or being sent to Zenodo,
    or failed - with whether another automatic attempt is still coming."""
    package_id = pkg_dict.get('id')
    if not package_id:
        return []
    return [
        {
            'id': upload.id,
            'filename': upload.filename,
            'status': upload.status,
            'error': upload.error,
            'will_retry': (upload.send_attempts or 0) < MAX_SEND_ATTEMPTS,
        }
        for upload in ZenodoUploadQuery.for_package(package_id)
    ]
