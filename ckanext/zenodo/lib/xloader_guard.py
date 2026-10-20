"""Keeps ckanext-xloader from trying to load a Zenodo-backed Link
resource into the datastore - via its own IXloader.can_upload hook,
the one extension point it offers for exactly this (see
ckanext-xloader/ckanext/xloader/interfaces.py's own docstring: "can be
used to stop the processing of the xloader submit function").

Found via live testing: once publish_guard._create_zenodo_resources
started setting a real `format` on these resources (needed so the
resource's own icon shows something other than the generic 'data' one
- see publish_guard.py's own docstring), that format started matching
xloader's own `ckanext.xloader.formats` list (CSV is in there, see
CLAUDE.md), which makes xloader's IResourceController.after_resource_create
(and, for an update whose url changed, IDomainObjectModification.notify)
auto-submit the resource for loading - unconditionally, with no way to
opt out short of this interface. It then tries to download `url`
(Zenodo's own HTML landing page, not the real file) as if it were CSV
data, fails ("Tabulator error: Cannot parse the source ... using
charmap encoding"), and leaves a red "Datastore error" badge on every
Zenodo CSV resource - confirmed exactly matching what the user saw,
right after the icon fix (and, separately, after a one-off backfill
script patched the already-existing resources' own format/url, which
also triggers notify() via its own url_changed check).

A soft dependency, same reasoning as publish_guard.py's own fair3r
import: ckanext-xloader not being installed must not break plugin
loading - see plugin.py's own conditional `plugins.implements`,
mirroring xloader's own plugin.py's handling of the optional
IDataDictionaryForm.
"""

import logging

from ckan.plugins import toolkit

from ckanext.zenodo.lib.publish_guard import _zenodo_record_url
from ckanext.zenodo.model import ZenodoDepositionQuery

log = logging.getLogger(__name__)


def can_upload(resource_id):
    """IXloader.can_upload - returning False stops xloader from ever
    submitting this resource, independently of its own format check
    (which runs first, and is what made this necessary - see this
    module's own docstring). True for everything that isn't actually
    one of our own Zenodo-backed resources, so this never affects a
    dataset's real CKAN-uploaded files.

    Matched the same way maybe_delete_published_file does (name in the
    deposition's recorded filenames AND the url actually points at
    that same concept record) rather than by name alone - an unrelated
    resource that merely happens to share a filename with one of ours
    would need the same url too to be mistaken for one."""
    try:
        resource = toolkit.get_action('resource_show')(
            {'ignore_auth': True}, {'id': resource_id}
        )
    except toolkit.ObjectNotFound:
        return True

    package_id = resource.get('package_id')
    filename = resource.get('name')
    if not package_id or not filename:
        return True

    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or not deposition.concept_record_id:
        return True

    record_url = _zenodo_record_url(deposition.concept_record_id, deposition.is_sandbox)
    if not (resource.get('url') or '').startswith(record_url):
        return True
    if filename not in ZenodoDepositionQuery.list_filenames(package_id):
        return True

    log.debug(
        'xloader_guard: rejecting xloader submission for Zenodo-backed '
        'resource %s (%s) of package %s - not real tabular data at that '
        'url',
        resource_id,
        filename,
        package_id,
    )
    return False
