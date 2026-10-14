"""Template helpers - unlike the rest of lib/, this one is deliberately
CKAN-only (reads the model via ZenodoDepositionQuery, not just pure
computation), since its only job is feeding the dataset page template.
"""

from ckanext.zenodo.model import ZenodoDepositionQuery


def zenodo_deposition_for_package(package_id):
    """Returns a dict describing this package's Zenodo deposition for
    template use, or None if there isn't one at all (the common case -
    most datasets never touch Zenodo).

    Shown on the dataset page regardless of `published`: a pending (not
    yet published) deposition still means "there's real data for this
    dataset, it's just not minted on Zenodo yet" - a much less confusing
    message than CKAN's own "This dataset has no data" for the in-between
    moment right after a file was uploaded to Zenodo but before the
    dataset itself transitions draft -> active and the publish job runs.
    """
    deposition = ZenodoDepositionQuery.read_package(package_id)
    if deposition is None or deposition.file_count <= 0:
        return None

    record_url = None
    if deposition.published and deposition.zenodo_record_id:
        host = 'sandbox.zenodo.org' if deposition.is_sandbox else 'zenodo.org'
        record_url = f'https://{host}/records/{deposition.zenodo_record_id}'

    return {
        'published': deposition.published,
        'doi': deposition.zenodo_record_doi,
        'record_url': record_url,
        'file_count': deposition.file_count,
        'is_sandbox': deposition.is_sandbox,
    }
