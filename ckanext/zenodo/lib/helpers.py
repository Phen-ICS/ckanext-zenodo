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
"""

from ckan.plugins import toolkit

from ckanext.zenodo.model import ZenodoDepositionQuery

# Keyed by the short code ZenodoDepositionQuery.set_notice() stores (see
# publish_guard.py/delete_zenodo_file.py's own call sites) - a plain string
# column, not a DB enum, so a new notice kind only needs an entry here, no
# migration.
_NOTICE_MESSAGES = {
    'published': toolkit._('Archived on Zenodo'),
    'new_version_published': toolkit._('Zenodo archive updated'),
}


def zenodo_notice_for_package(pkg_dict):
    """A one-shot "flash message, but surviving past this request" -
    see ZenodoDepositionQuery.set_notice's own docstring for why a real
    flash_success() (session-based, same request only - see
    ckanext-doi's own synchronous equivalent) can't be used for
    something a deferred RQ job wants to announce. Returns None (the
    common case) if there's nothing to show; otherwise a dict
    {'message': ...} and clears it, so it only ever shows once."""
    package_id = pkg_dict.get('id')
    if not package_id:
        return None

    notice = ZenodoDepositionQuery.pop_notice(package_id)
    if notice is None:
        return None

    message = _NOTICE_MESSAGES.get(notice, notice)
    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition and deposition.zenodo_record_doi:
        message = f'{message} (DOI: {deposition.zenodo_record_doi})'
    return {'message': f'{message}.'}


def zenodo_pending_deposition_for_package(pkg_dict):
    """Returns None if there's nothing pending (no deposition, no files
    yet, or already published - a real resource exists for it by then).
    Otherwise a dict {'waiting_for_public': bool, 'filenames': [...]} -
    the template picks the right message from the former, and lists the
    latter (the real files already on Zenodo, read back from the local
    row rather than a live Zenodo API call on every page render - see
    ZenodoDepositionQuery.add_file, called by the upload blueprint)."""
    package_id = pkg_dict.get('id')
    if not package_id:
        return None

    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.published:
        return None
    if deposition.file_count <= 0:
        return None

    return {
        'waiting_for_public': bool(pkg_dict.get('private')),
        'filenames': ZenodoDepositionQuery.list_filenames(package_id),
    }
