"""Deletes one file from a dataset's Zenodo deposition - they're the
user's own files, theirs to remove, before or after publication. The
two cases are genuinely different operations, not the same code path
with a flag:

- **Before publication** (the deposition is still a draft on Zenodo): a
  direct delete from that same, still-unpublished deposition.
  delete_pending_file() is called synchronously from the upload
  blueprint's own delete route (zenodo_upload.py) - a single external
  HTTP call, same cost class as that blueprint's own synchronous upload
  call.

- **After publication** (DOI already minted): a published Zenodo record
  is immutable by design (DOIs are meant to be permanent) - there is no
  "delete a file from a published record" endpoint. Removing a file
  here instead means a new-version-and-republish workflow
  (create_new_version -> delete the file from that draft -> publish it
  - see zenodo_client.py's own docstring above create_new_version for
  what was confirmed empirically about this on the real sandbox). This
  is triggered by CKAN's own, already-existing "delete resource"
  button/action (via maybe_delete_published_file,
  IResourceController.before_resource_delete in plugin.py) rather than
  a bespoke one, and is deferred to its own RQ job - chaining 3
  external calls is too slow/risky to run synchronously from inside
  that hook, same reasoning as publish_guard.py's own deferred publish
  job.

**Known limitation, prevented rather than auto-handled**: Zenodo
rejects publishing a version with zero files (confirmed empirically:
"Missing uploaded files... mark it as metadata-only") - deleting the
*last* remaining file of an already-published deposition would fail
to produce a new Zenodo version, silently (by the time the deferred
job runs and fails, CKAN's own resource_delete has already removed the
resource - there's no request/response left to show anything in).
lib/auth.py's own resource_delete override denies this specific case
up front instead, with a clear reason, so the inconsistency this
module's own fallback branch below logs about should no longer happen
in practice - that branch stays as a defensive no-op path (e.g. a
sysadmin bypassing auth, or a race between two concurrent deletes) in
case something gets past that check some other way, not because this
is still expected to happen in normal use.
"""

import logging

from ckan.model import repo
from ckan.plugins import toolkit

from ckanext.zenodo.lib import zenodo_client, zenodo_config
from ckanext.zenodo.lib.publish_guard import (
    _zenodo_record_url,
    accept_pending_community_inclusion,
)
from ckanext.zenodo.model import ZenodoDepositionQuery

log = logging.getLogger(__name__)


def _find_file_id(deposition_id, filename, token, sandbox):
    """Zenodo's delete-file endpoint needs its own internal file id, not
    just the filename - looked up from the deposition's current files
    list."""
    deposition = zenodo_client.get_deposition(deposition_id, token, sandbox=sandbox)
    if deposition is None:
        return None
    for file_entry in deposition.get('files') or []:
        if file_entry.get('filename') == filename:
            return file_entry.get('id')
    return None


def delete_pending_file(package_id, filename):
    """Deletes one file from a dataset's still-unpublished Zenodo
    deposition. Returns True on success, False if there's nothing to
    do (no deposition, already published - see
    maybe_delete_published_file for that case instead - or this
    filename isn't actually recorded for it). Can raise
    zenodo_client.ZenodoError, which the caller (the upload blueprint)
    turns into a 502 - unlike the post-publish path, there's no deferred
    job here to swallow it into, since this runs synchronously inside
    the blueprint's own request same as the upload call does."""
    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.published:
        return False
    if filename not in ZenodoDepositionQuery.list_filenames(package_id):
        return False

    sandbox = deposition.is_sandbox
    token = zenodo_config.access_token(sandbox)

    file_id = _find_file_id(deposition.zenodo_deposition_id, filename, token, sandbox)
    if file_id is not None:
        zenodo_client.delete_deposition_file(
            deposition.zenodo_deposition_id, file_id, token, sandbox=sandbox
        )
    # Removed from our own bookkeeping even if Zenodo had already lost
    # track of it somehow (e.g. a previous attempt succeeded on
    # Zenodo's side but crashed before this local update ran) - nothing
    # left to point at, no reason to keep listing it as pending.
    ZenodoDepositionQuery.remove_file(package_id, filename)
    repo.commit_and_remove()
    return True


def maybe_delete_published_file(context, resource, resources):
    """Call from IResourceController.before_resource_delete. Cheap no-op
    for every normal resource deletion; only acts when the resource
    being deleted is actually one of this plugin's own Zenodo-backed
    resources (matched by filename against what's on record for this
    package's deposition - a resource with the same name CKAN happens
    to have for an unrelated reason would need the *same* url too,
    which _create_zenodo_resources already guarantees uniquely
    identifies one of ours).

    `resource` only carries an id at this hook - the full dict (name,
    url, package_id) is looked up from `resources`, the dataset's own
    current resource list CKAN passes alongside it."""
    full_resource = next(
        (r for r in resources or [] if r.get('id') == resource.get('id')), None
    )
    if full_resource is None:
        return

    package_id = full_resource.get('package_id')
    filename = full_resource.get('name')
    if not package_id or not filename:
        return

    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or not deposition.published:
        return
    record_url = _zenodo_record_url(
        deposition.concept_record_id or '', deposition.is_sandbox
    )
    if full_resource.get('url') != record_url:
        return
    if filename not in ZenodoDepositionQuery.list_filenames(package_id):
        return

    toolkit.enqueue_job(
        _delete_published_file,
        kwargs={'package_id': package_id, 'filename': filename},
        title=f'ckanext-zenodo delete_zenodo_file: {filename} from {package_id}',
    )


def _delete_published_file(package_id, filename):
    """Runs as its own RQ job - see this module's own docstring for why
    (chains 3 external calls, too slow/risky from inside the
    before_resource_delete hook itself)."""
    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or not deposition.published:
        return
    if filename not in ZenodoDepositionQuery.list_filenames(package_id):
        # Already handled (e.g. this job was retried after already
        # succeeding on a previous attempt).
        return

    sandbox = deposition.is_sandbox
    token = zenodo_config.access_token(sandbox)

    try:
        new_draft = zenodo_client.create_new_version(
            deposition.zenodo_deposition_id, token, sandbox=sandbox
        )
        file_id = _find_file_id(new_draft['id'], filename, token, sandbox)
        if file_id is not None:
            zenodo_client.delete_deposition_file(
                new_draft['id'], file_id, token, sandbox=sandbox
            )
        result = zenodo_client.publish_deposition(
            new_draft['id'], token, sandbox=sandbox
        )
    except zenodo_client.ZenodoError:
        log.exception(
            'delete_zenodo_file: failed to publish a new Zenodo version '
            'after removing %r from package %s - the previous published '
            'version is unaffected (still has this file, frozen under its '
            'own DOI); its CKAN resource is already gone, though (CKAN ran '
            'its own resource_delete before this job got a chance to '
            'object), so Data and Resources may now list fewer files than '
            'Zenodo actually still has until this is retried or resolved '
            'another way',
            filename,
            package_id,
        )
        return

    record_id = result.get('record_id') or result.get('id')
    accept_pending_community_inclusion(record_id, token, sandbox, package_id)

    ZenodoDepositionQuery.remove_file(package_id, filename)
    ZenodoDepositionQuery.update_package(
        package_id,
        zenodo_deposition_id=new_draft['id'],
        zenodo_record_id=record_id,
        zenodo_record_doi=result.get('doi'),
    )
    # Own RQ job, own transaction - same reasoning as publish_guard.py's
    # own repo.commit_and_remove().
    repo.commit_and_remove()
