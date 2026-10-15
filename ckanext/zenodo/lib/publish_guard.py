"""Publishes a dataset's pending Zenodo deposition (mints the real DOI)
the moment the dataset itself transitions draft -> active - mirrors how
ckanext-doi already defers its own DOI minting to that same moment via
sync_datacite_metadata, and uses the same deferred-job pattern
ckanext-fair3r's draft_guard.py established: the hook fires from inside
the caller's own still-open transaction, and a slow external HTTP call
(or another top-level action's own commit) can't run synchronously from
in there - see draft_guard.py's own docstring for the concrete
ResourceClosedError this caused when tried directly.

Unlike draft_guard.py's race, there's nothing to race against here: this
is a one-way "is there unpublished work to do" check, and Zenodo's own
publish endpoint is safe to call on an already-published deposition (an
error to catch and ignore, not a correction to make) - so no Redis
TTL-marker trick is needed, just the straightforward check below.
"""

import logging
from urllib.parse import quote_plus

from ckan.lib.helpers import unified_resource_format
from ckan.model import repo
from ckan.plugins import toolkit

from ckanext.zenodo.lib import zenodo_client, zenodo_config
from ckanext.zenodo.lib.metadata_mapper import datacite_to_zenodo
from ckanext.zenodo.model import ZenodoDepositionQuery

log = logging.getLogger(__name__)


def maybe_publish_pending_deposition(pkg_dict):
    """Call from IPackageController.after_dataset_update. Cheap no-op for
    every normal update; only acts when there's genuinely a pending
    deposition to publish.

    Same trigger condition as ckanext-doi's own after_dataset_update
    (active AND not private, not just active) - minting a DOI, on
    Zenodo same as on DataCite, is a one-way action, so a dataset that's
    active but still private should never get one yet. Firing on every
    update (not just the draft->active transition) is what this
    requires: a dataset created active+private and made public only
    later gets published the next time it's updated (even just
    toggling that flag), not just once right after creation - exactly
    mirroring ckanext-doi's own behaviour, and incidentally also what
    keeps access_right (see publish_guard._publish_deposition) from
    ever going stale: by the time this runs, the dataset is always
    already public, so it's always 'open', never a 'restricted' record
    that CKAN later forgot to reconcile."""
    if pkg_dict.get('type', 'dataset') != 'dataset':
        return
    if pkg_dict.get('state') != 'active':
        return
    if pkg_dict.get('private', False):
        return

    package_id = pkg_dict.get('id')
    if not package_id:
        return

    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.published:
        return
    if deposition.file_count <= 0:
        # Never publish an empty deposition - Zenodo would reject it
        # anyway (a DOI needs at least one file), and this is also the
        # common case: most datasets have no Zenodo deposition at all.
        return

    toolkit.enqueue_job(
        _publish_deposition,
        kwargs={'package_id': package_id},
        title=f'ckanext-zenodo publish_guard: publish {package_id}',
    )


def _fdf_output_json(pkg_dict):
    for extra in pkg_dict.get('extras') or []:
        key = extra.get('key') if isinstance(extra, dict) else None
        if key == 'fdf_output_json':
            return extra.get('value') if isinstance(extra, dict) else None
    return None


def _extract_datacite_metadata(pkg_dict):
    """FDF metadata, via ckanext-fair3r's own converter - a soft
    dependency (try/except ImportError), same pattern
    ckanext-citations' helpers.py uses for completeness_for_package: this
    plugin's real purpose is tied to FAIR3R's FDF form, but a dataset
    with real files already uploaded should still get *something*
    published on Zenodo even without it (fair3r not installed, or no
    fdf_output_json extra at all) rather than silently failing to
    publish."""
    fdf_output_json = _fdf_output_json(pkg_dict)
    if not fdf_output_json:
        return {}

    try:
        from ckanext.fair3r.lib.fdf.datacite_converter import DataCiteConverter
    except ImportError:
        log.warning(
            'ckanext-fair3r not installed - publishing minimal Zenodo '
            'metadata for package %s (title/submitter only)',
            pkg_dict.get('id'),
        )
        return {}

    try:
        return DataCiteConverter.fdf_to_datacite(fdf_output_json)
    except ValueError:
        log.exception(
            'Failed to parse fdf_output_json for package %s - publishing '
            'minimal Zenodo metadata instead',
            pkg_dict.get('id'),
        )
        return {}


def _submitter_display_name(pkg_dict):
    """The real person who created the dataset (not the shared Zenodo
    account that actually performs the deposit) - see the implementation
    plan's requirement that the description always names them."""
    creator_user_id = pkg_dict.get('creator_user_id')
    if creator_user_id:
        try:
            user = toolkit.get_action('user_show')(
                {'ignore_auth': True}, {'id': creator_user_id}
            )
            if user.get('display_name'):
                return user['display_name']
        except toolkit.ObjectNotFound:
            pass
    return pkg_dict.get('author') or 'a FAIR3R user'


def _publish_deposition(package_id):
    """Runs as its own RQ job - see this module's docstring for why this
    can't just be called straight from the hook above."""
    try:
        pkg_dict = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': package_id}
        )
    except toolkit.ObjectNotFound:
        log.warning(
            'publish_guard: package %s not found - skipping publish', package_id
        )
        return

    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.published:
        return

    datacite_metadata = _extract_datacite_metadata(pkg_dict)
    metadata = datacite_to_zenodo(
        datacite_metadata,
        title=pkg_dict.get('title') or pkg_dict.get('name'),
        submitter_display_name=_submitter_display_name(pkg_dict),
        community_identifier=zenodo_config.community_identifier(),
        # maybe_publish_pending_deposition now only enqueues this job
        # once the dataset is active AND public, so this is always
        # 'open' in practice by the time it runs - kept explicit (not
        # hardcoded) in case this is ever called some other way.
        access_right='restricted' if pkg_dict.get('private') else 'open',
        tags=pkg_dict.get('tags'),
        license_id=pkg_dict.get('license_id'),
    )

    sandbox = deposition.is_sandbox
    token = zenodo_config.access_token(sandbox)

    try:
        zenodo_client.update_deposition_metadata(
            deposition.zenodo_deposition_id, metadata, token, sandbox=sandbox
        )
        result = zenodo_client.publish_deposition(
            deposition.zenodo_deposition_id, token, sandbox=sandbox
        )
    except zenodo_client.ZenodoError:
        log.exception(
            'publish_guard: failed to publish Zenodo deposition for package %s',
            package_id,
        )
        return

    record_id = result.get('record_id') or result.get('id')
    accept_pending_community_inclusion(record_id, token, sandbox, package_id)
    # The *concept* record - shared by every version of this deposition,
    # never any one version's own id - is what CKAN resources point at
    # (see _zenodo_record_url's own docstring for why): confirmed
    # empirically that /records/{conceptrecid} always redirects to
    # whichever version is latest, so these resources need no update at
    # all when a later version is published (e.g. after deleting a
    # file - see delete_zenodo_file.py).
    concept_record_id = result.get('conceptrecid') or record_id
    _create_zenodo_resources(
        pkg_dict, concept_record_id, result.get('doi'), sandbox, result.get('files')
    )

    ZenodoDepositionQuery.mark_published(
        package_id,
        result.get('id'),
        result.get('doi'),
        concept_record_id=concept_record_id,
    )
    # A real flash_success() has nothing to attach to by now (this is a
    # deferred job, not the request that enqueued it) - see
    # lib/helpers.py's zenodo_notice_for_package for the DB-backed
    # equivalent this sets instead, shown once on the dataset page's
    # next render.
    ZenodoDepositionQuery.set_notice(package_id, 'published')
    # Own RQ job, own transaction - nothing else commits this for us, same
    # reasoning as the upload blueprint's own repo.commit_and_remove().
    # resource_create above already committed itself (CKAN actions always
    # do) - this only covers mark_published's own model write.
    repo.commit_and_remove()


def _zenodo_record_url(concept_record_id, sandbox):
    """The *concept* record's own landing page - shared by every version
    of a deposition, never any one version's own record id. Confirmed
    empirically against the real sandbox: this URL (and the concept DOI
    that resolves to it) always redirects to whichever version is
    currently latest, so a CKAN resource pointing at it stays correct
    forever, with no update needed, even after a later version is
    published (e.g. after deleting a file - see
    lib/delete_zenodo_file.py)."""
    host = 'sandbox.zenodo.org' if sandbox else 'zenodo.org'
    return f'https://{host}/records/{concept_record_id}'


def _zenodo_file_preview_url(concept_record_id, sandbox, filename):
    """Same concept-record URL as _zenodo_record_url (so it stays correct
    across later versions, exactly as that one does), plus a
    `?preview_file=` query string that makes Zenodo's own landing page
    open straight to *this* file's preview instead of the record's
    default (usually just the first file) - confirmed empirically
    against the real sandbox, e.g. `test (Copie 3).csv` ->
    `?preview_file=test+%28Copie+3%29.csv` (quote_plus: space -> '+',
    everything else percent-encoded - matches exactly)."""
    return f'{_zenodo_record_url(concept_record_id, sandbox)}?preview_file={quote_plus(filename)}'


def _format_for_filename(filename):
    """CKAN's own resource_item.html only shows a real format icon (vs.
    the generic 'data' one) when res.format is set - resource_create
    never gets a chance to guess it the way an actual upload does (the
    url here is Zenodo's record page, which has no file extension of
    its own to guess from). Derives it from the filename's own extension
    instead, normalized through CKAN's own format table (e.g. 'csv' ->
    'CSV') so it matches exactly what a native CKAN upload of the same
    file type would show - confirmed via unified_resource_format, which
    just returns unknown extensions back unchanged rather than raising."""
    if '.' not in filename:
        return ''
    return unified_resource_format(filename.rsplit('.', 1)[-1])


def _create_zenodo_resources(pkg_dict, concept_record_id, doi, sandbox, files):
    """Creates one genuine CKAN Link resource per file actually uploaded
    to Zenodo - confirmed, via actual testing, to be a better fit than a
    bespoke display-only panel outside the resource list: a real
    resource shows up in the normal "Data and Resources" list with no
    template override needed, and - more importantly - is picked up by
    every feature that already iterates pkg.resources (DCAT/schema.org/
    Croissant export), which a panel bolted on elsewhere never would be.

    One resource per file (not one generic "Archived on Zenodo" entry
    for the whole deposition) so each shows its own real filename in
    "Data and Resources" - a dataset can have several files in the same
    Zenodo deposition (see get_or_create_open_deposition). Each one's
    `url` is the *concept* record's landing page (stable across
    versions - see _zenodo_record_url - not any one version's own
    record id) plus a `?preview_file=` query string naming that
    specific file (see _zenodo_file_preview_url) - confirmed
    empirically that Zenodo's own landing page uses this to open
    straight to that file's own preview/download rather than its
    default (usually just the first file in the deposition), so CKAN's
    own "Go to resource" link lands somewhere file-specific instead of
    the same generic record page for every one of them.
    `files` is the `files` list a publish_deposition()/get_deposition()
    response already carries ({filename, filesize, checksum, ...} per
    file) - no separate API call or local bookkeeping needed for this.

    Idempotent by (name, url-prefix) rather than a new DB column: if a
    resource for this exact filename+record already exists (e.g. this
    job was retried after a crash right after this step, on a previous
    run), nothing new is created for it - matched by prefix, not exact
    equality, so this also recognizes resources created by an earlier
    version of this code that didn't yet have the per-file
    `?preview_file=` suffix. Never fatal - the DOI is already minted by
    the time this runs; a failure here just means the record stays
    correctly published on Zenodo without (yet) a CKAN-side resource
    for some or all of its files."""
    if concept_record_id is None or not files:
        return

    record_url = _zenodo_record_url(concept_record_id, sandbox)
    existing_names = {
        r.get('name')
        for r in pkg_dict.get('resources') or []
        if (r.get('url') or '').startswith(record_url)
    }

    description = f'Archived on Zenodo (DOI: {doi}).' if doi else 'Archived on Zenodo.'
    if sandbox:
        description += ' Sandbox record - not a permanent archive.'

    # resource_create -> package_update fires CKAN's activity-tracking
    # signal (ckanext.activity.subscriptions), which needs context['user']
    # to resolve to a *real* user - ignore_auth alone only skips the
    # permission check, it doesn't give the activity subscriber anyone to
    # attribute the change to. Confirmed empirically: omitting this raised
    # ValidationError({'user_id': ['User not found']}) from deep inside
    # resource_create, from this RQ job (no web request => no "current
    # user" at all) - the standard fix for a background job that needs to
    # act as *someone* is CKAN's own site user, same as e.g. search
    # reindexing jobs use.
    try:
        site_user = toolkit.get_action('get_site_user')({'ignore_auth': True}, {})
    except Exception:
        log.exception(
            'publish_guard: failed to resolve the site user for package %s '
            '(concept record %s) - skipping Zenodo Link resource creation',
            pkg_dict.get('id'),
            concept_record_id,
        )
        return

    for file_entry in files:
        filename = (file_entry.get('filename') or '').strip()
        if not filename or filename in existing_names:
            continue
        try:
            toolkit.get_action('resource_create')(
                {'ignore_auth': True, 'user': site_user['name']},
                {
                    'package_id': pkg_dict['id'],
                    'url': _zenodo_file_preview_url(
                        concept_record_id, sandbox, filename
                    ),
                    'name': filename,
                    'description': description,
                    'format': _format_for_filename(filename),
                },
            )
        except Exception:
            log.exception(
                'publish_guard: failed to create the Zenodo Link resource '
                'for file %r of package %s (concept record %s) - the '
                'deposition is published, just not reflected as a CKAN '
                'resource yet',
                filename,
                pkg_dict.get('id'),
                concept_record_id,
            )


def accept_pending_community_inclusion(record_id, token, sandbox, package_id):
    """Confirmed empirically against the real sandbox (Stage 8 smoke
    test): publishing a deposition whose metadata names a community only
    creates a pending "community-inclusion" request against the record -
    it does NOT associate the record with the community by itself, even
    for the account that owns that community. This accepts that one
    request so the record actually ends up listed under the community,
    matching what the plan's description always promised.

    Explicitly (re-)submits the inclusion request first (see
    zenodo_client.request_community_inclusion's own docstring) rather
    than assuming metadata.communities already created one - confirmed
    empirically that it only does so for a brand new deposition's
    *first* publish; a later version's publish (see
    lib/delete_zenodo_file.py) silently created no request at all,
    which would otherwise drop the community association for every
    version after the first.

    Best-effort and never fatal: the DOI is already minted by the time
    this runs, and a missed/failed acceptance here just means the
    record is a (findable, already-published) orphan with respect to
    the community until accepted manually or on a later retry - not a
    reason to roll anything back."""
    if not record_id:
        return
    try:
        zenodo_client.request_community_inclusion(
            record_id, zenodo_config.community_identifier(), token, sandbox=sandbox
        )
        requests_page = zenodo_client.list_record_requests(
            record_id, token, sandbox=sandbox
        )
        pending = next(
            (
                r
                for r in (requests_page or {}).get('hits', {}).get('hits', [])
                if r.get('type') == 'community-inclusion' and r.get('is_open')
            ),
            None,
        )
        if pending is None:
            return
        zenodo_client.accept_request(pending['id'], token, sandbox=sandbox)
    except zenodo_client.ZenodoError:
        log.exception(
            'publish_guard: failed to accept the pending community-inclusion '
            'request for package %s (record %s) - record is published but '
            'not yet listed under the community',
            package_id,
            record_id,
        )
