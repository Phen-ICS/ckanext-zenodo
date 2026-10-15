"""Prevents deleting the *last* remaining file of an already-published
Zenodo deposition - confirmed via real testing: Zenodo rejects
publishing a new version with zero files ("mark it as metadata-only",
see lib/delete_zenodo_file.py's own docstring), so the deferred job
that would otherwise do the new-version-and-republish dance fails
after CKAN's own resource_delete has already removed the resource -
silently, from the user's point of view (the job's own failure is
logged, but by then there's no request/response left to show anything
in). Denying the delete up front instead, with a clear reason, is much
better than letting it fail invisibly after the fact and leaving
Zenodo and CKAN's own resource list out of sync.

Chains onto CKAN's own resource_delete auth function rather than
replacing it outright - still needs package_delete permission first,
same as core; this only adds one more thing that can say no.
"""

from ckan.logic.auth import get_resource_object
from ckan.logic.auth.delete import resource_delete as _default_resource_delete
from ckan.plugins import toolkit

from ckanext.zenodo.model import ZenodoDepositionQuery


def resource_delete(context, data_dict):
    default_result = _default_resource_delete(context, data_dict)
    if not default_result.get('success'):
        return default_result

    resource = get_resource_object(context, data_dict)
    deposition = ZenodoDepositionQuery.read_package(resource.package_id)
    if deposition is None or not deposition.published:
        return default_result

    filenames = ZenodoDepositionQuery.list_filenames(resource.package_id)
    if resource.name in filenames and len(filenames) <= 1:
        return {
            'success': False,
            'msg': toolkit._(
                "Can't delete the last file of a published Zenodo record - "
                'Zenodo has no way to publish a version with no files at '
                'all. Add a replacement file first, or ask an '
                "administrator to resolve this dataset's Zenodo archive "
                'another way.'
            ),
        }
    return default_result
