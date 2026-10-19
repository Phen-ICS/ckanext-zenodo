"""What happens on Zenodo when a dataset is purged or made private.

Both are chained CKAN actions rather than IPackageController hooks:
dataset_purge has no plugin hook at all (and the fair_zenodo_deposition
row goes with the package via ON DELETE CASCADE, so it has to be read
*before* the purge runs), and "was public, is now private" needs the
dataset's visibility from before package_update, which no hook gets.

- Purge, never published: the Zenodo draft has no DOI, nothing
  permanent is lost - deleted, so it doesn't sit forever on the shared
  account's quota. Not done on a plain delete: CKAN's trash can still
  restore the dataset until it's purged.
- Purge, published: a published Zenodo record can't be deleted - the
  user is told it stays public. A pending new-version draft on top of
  it (a file being added) is discarded.
- Public -> private: same reason, the already-archived files stay
  public on Zenodo - the user is told so. Nothing is done on Zenodo.

Flash messages only when there's a request to show them in (not from
the API/CLI).
"""

import logging

from ckan import model
from ckan.plugins import toolkit
from flask import has_request_context

from ckanext.zenodo.lib import zenodo_client, zenodo_config
from ckanext.zenodo.lib.publish_guard import _zenodo_record_url
from ckanext.zenodo.model import ZenodoDepositionQuery

log = logging.getLogger(__name__)


def _record_link(deposition):
    url = _zenodo_record_url(deposition.concept_record_id, deposition.is_sandbox)
    return f'{url} (DOI {deposition.zenodo_record_doi})'


@toolkit.chained_action
def dataset_purge(original_action, context, data_dict):
    pkg = model.Package.get(data_dict.get('id'))
    deposition = ZenodoDepositionQuery.read_package(pkg.id) if pkg else None
    # Plain values, not the ORM object - the row is gone after the purge.
    snapshot = (
        {
            'title': pkg.title or pkg.name,
            'deposition_id': deposition.zenodo_deposition_id,
            'sandbox': deposition.is_sandbox,
            'published': deposition.published,
            'ever_published': bool(deposition.concept_record_id),
            'record_link': _record_link(deposition)
            if deposition.concept_record_id
            else None,
        }
        if deposition
        else None
    )

    result = original_action(context, data_dict)

    if snapshot is None:
        return result
    if not snapshot['published']:
        toolkit.enqueue_job(
            _remove_unpublished_draft,
            kwargs={
                'deposition_id': snapshot['deposition_id'],
                'sandbox': snapshot['sandbox'],
                'ever_published': snapshot['ever_published'],
            },
            title=f'ckanext-zenodo: remove draft {snapshot["deposition_id"]}',
        )
    if snapshot['ever_published'] and has_request_context():
        toolkit.h.flash_notice(
            toolkit._(
                '"{title}" was purged, but its files stay publicly archived on '
                "Zenodo: {record}. A published Zenodo record can't be deleted "
                'from here - only Zenodo support can remove it.'
            ).format(title=snapshot['title'], record=snapshot['record_link'])
        )
    return result


def _remove_unpublished_draft(deposition_id, sandbox, ever_published):
    """RQ job. Never published -> delete the whole draft. A new-version
    draft of a published record -> discard just that draft."""
    token = zenodo_config.access_token(sandbox)
    try:
        if ever_published:
            zenodo_client.discard_deposition(deposition_id, token, sandbox=sandbox)
        else:
            zenodo_client.delete_deposition(deposition_id, token, sandbox=sandbox)
    except zenodo_client.ZenodoError:
        log.exception(
            'Could not remove Zenodo draft %s after its dataset was purged - '
            'delete it by hand from the shared account',
            deposition_id,
        )


@toolkit.chained_action
def package_update(original_action, context, data_dict):
    pkg = model.Package.get(data_dict.get('id') or data_dict.get('name'))
    was_public = pkg is not None and not pkg.private

    result = original_action(context, data_dict)

    if was_public and has_request_context():
        pkg = model.Package.get(pkg.id)
        deposition = ZenodoDepositionQuery.read_package(pkg.id)
        if pkg.private and deposition and deposition.concept_record_id:
            toolkit.h.flash_notice(
                toolkit._(
                    '"{title}" is now private, but its files already archived '
                    'on Zenodo stay public: {record}. A published Zenodo record '
                    "can't be made private."
                ).format(title=pkg.title or pkg.name, record=_record_link(deposition))
            )
    return result
