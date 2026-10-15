"""Template helper - CKAN-only on purpose (reads the model via
ZenodoDepositionQuery), unlike the rest of lib/.

Only covers the transient gap between a file reaching Zenodo and the
real CKAN resource existing for it (see publish_guard.py's
_create_zenodo_resource): the upload itself is synchronous, but turning
it into a published Zenodo record + a CKAN Link resource is a deferred
RQ job, enqueued on the dataset's draft -> active transition. Someone
landing on the dataset page in that window would otherwise see CKAN's
own plain "This dataset has no data" right after successfully uploading
a file - this says "hang on" instead. Once the job runs, a real resource
exists and this helper stops returning anything (the dataset no longer
has a pending, unpublished deposition), so there is nothing left to
show here and no risk of it ever duplicating the real resource.
"""

from ckanext.zenodo.model import ZenodoDepositionQuery


def zenodo_pending_deposition_for_package(package_id):
    """True-ish iff there is real, uploaded data waiting for the
    publish job to turn it into a CKAN resource - never true once that
    has happened."""
    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.published:
        return False
    return deposition.file_count > 0
