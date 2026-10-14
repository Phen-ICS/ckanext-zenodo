"""Centralises reading ckanext.zenodo.* CKAN config - the one place in
this plugin (besides the blueprint and the publish hook, which already
need `ckan` for other reasons) that reads it, so the upload blueprint
(stage 5) and the publish hook (stage 6) stay consistent instead of each
inventing their own defaults."""

from ckan.plugins import toolkit

DEFAULT_MAX_UPLOADS_PER_HOUR = 20
# Generous on purpose - Zenodo's own per-file limit is the real ceiling,
# this is just a sanity cap against a single user exhausting the shared
# account's quota, not a routing threshold (that's ckan.max_resource_size).
DEFAULT_MAX_BYTES_PER_HOUR = 5 * 1024**3  # 5 GB
DEFAULT_MAX_FILE_SIZE_BYTES = 50 * 1024**3  # 50 GB, Zenodo's own limit
DEFAULT_COMMUNITY_IDENTIFIER = 'fair3r'


def sandbox_mode():
    """Whether to talk to the sandbox instead of production Zenodo.
    Defaults to True - this plugin is built and tested against the
    sandbox first; production is an explicit, later opt-in."""
    return toolkit.asbool(toolkit.config.get('ckanext.zenodo.test_mode', True))


def access_token(sandbox=None):
    if sandbox is None:
        sandbox = sandbox_mode()
    key = (
        'ckanext.zenodo.sandbox_access_token'
        if sandbox
        else 'ckanext.zenodo.access_token'
    )
    token = toolkit.config.get(key)
    if not token:
        raise RuntimeError(f'{key} is not configured')
    return token


def community_identifier():
    return toolkit.config.get(
        'ckanext.zenodo.community_identifier', DEFAULT_COMMUNITY_IDENTIFIER
    )


def max_uploads_per_hour():
    return toolkit.asint(
        toolkit.config.get(
            'ckanext.zenodo.max_uploads_per_hour', DEFAULT_MAX_UPLOADS_PER_HOUR
        )
    )


def max_bytes_per_hour():
    return toolkit.asint(
        toolkit.config.get(
            'ckanext.zenodo.max_bytes_per_hour', DEFAULT_MAX_BYTES_PER_HOUR
        )
    )


def max_file_size_bytes():
    return toolkit.asint(
        toolkit.config.get(
            'ckanext.zenodo.max_file_size_bytes', DEFAULT_MAX_FILE_SIZE_BYTES
        )
    )


def ckan_max_resource_size_bytes():
    """CKAN's own cap on CKAN-stored resources (ckan.max_resource_size,
    in MB) - the routing threshold: bigger than this and a file has
    nowhere to go but Zenodo."""
    return toolkit.asint(toolkit.config.get('ckan.max_resource_size', 10)) * 1024 * 1024


def xloader_formats():
    return (toolkit.config.get('ckanext.xloader.formats') or '').split()


def routing_config_dict():
    """What the client-side shouldRouteToZenodo() (stage 3, landing for
    real in ckanext-fair3r's resource-upload.js at stage 7) needs to
    decide, without a network round trip, whether a given file belongs on
    Zenodo. Deliberately a plain dict, not a registered CKAN template
    helper: ckanext-fair3r's own base.html calls this via a direct,
    try/except ImportError'd Python import instead (same pattern already
    used for citations_completeness in ckanext-citations) rather than
    `h.zenodo_routing_config_json()`, since CKAN has no
    `h.helper_available(...)` to check for a *possibly-unloaded* other
    plugin's helper before calling it from a template - see the
    implementation plan's cross-extension notes."""
    return {
        'max_resource_size_bytes': ckan_max_resource_size_bytes(),
        'xloader_formats': xloader_formats(),
    }
