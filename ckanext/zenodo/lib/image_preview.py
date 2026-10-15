"""Keeps a Zenodo-backed image resource's own core "Image" view pointing
at a real, directly-loadable image URL - instead of the broken one
CKAN auto-creates by default.

Found via live testing (the exact scenario the user asked about):
publish_guard._create_zenodo_resources sets `format` on each resource
(needed for the right icon - see publish_guard.py's own docstring),
and CKAN's own default-view auto-creation then adds an "Image" view
for anything whose format matches `ckan.preview.image_formats` -
pointing it at `resource.url` by default, which here is Zenodo's own
HTML landing page (see _zenodo_file_preview_url), not actual image
bytes. The result: `<img src="...">` that page's own template renders
just shows a broken image icon.

Can't fix this by changing `resource.url` itself - that field is
deliberately the *concept* record's own stable landing page (see
_zenodo_record_url's own docstring: stays correct forever, across
later versions, with no update needed) and is relied on elsewhere
(DCAT/schema.org/Croissant export, "Go to resource"). Embedding
Zenodo's own landing page in an <iframe> instead isn't an option
either - confirmed empirically that Zenodo sends
`X-Frame-Options: sameorigin`, so browsers refuse to frame it from a
different origin at all.

What *does* work, confirmed empirically against the real sandbox:
Zenodo's newer records API exposes each file's raw content directly -
`https://{host}/api/records/{record_id}/files/{filename}/content` -
with a real `image/png` (etc.) content-type and no CORS/framing
restriction that applies to a plain <img> tag (X-Frame-Options only
ever applies to framing a *document*, which <img> never does). Core's
own image_view already supports pointing at a URL other than
resource.url for exactly this kind of case, via its own `image_url`
schema field (see ckanext.imageview.plugin.ImageView.info) - this
module just keeps that field in sync, rather than reinventing a new
resource view type.

Unlike resource.url, this per-file content link is tied to one
specific *version*'s own record id, not the stable concept id, and
does go stale across a later version (e.g. after deleting a different
file - see delete_zenodo_file.py). Both publish_guard.py (first
publish) and delete_zenodo_file.py (republish after a delete) call
sync_image_preview with each publish's own fresh record id, so this
self-heals on every (re)publish rather than needing to stay correct
forever on its own.
"""

import logging
from urllib.parse import quote

from ckan.plugins import toolkit

log = logging.getLogger(__name__)


def zenodo_file_content_url(record_id, sandbox, filename):
    """The *version-specific* record's own direct file content link -
    deliberately not using whatever `links.download` the legacy
    deposit API's own `files` list reports: confirmed empirically that
    it's stale/wrong even right after a real publish (still pointing
    at a `.../draft/files/...` path that 404s). Built directly instead,
    matching the one confirmed working via a real sandbox request."""
    host = 'sandbox.zenodo.org' if sandbox else 'zenodo.org'
    return f'https://{host}/api/records/{record_id}/files/{quote(filename)}/content'


def _is_image_format(fmt):
    image_formats = (toolkit.config.get('ckan.preview.image_formats') or '').split()
    return (fmt or '').lower() in {f.lower() for f in image_formats}


def sync_image_preview(resource_id, fmt, content_url, user_name):
    """Ensures exactly one Image view on this resource points at
    `content_url` - updates CKAN's own auto-created one if present,
    creates one otherwise (defensive - core's default-view creation
    should already have made one for anything with an image format).
    No-op for a non-image format, or if content_url isn't known yet.

    `user_name` must be a real user (e.g. the site user - see
    publish_guard.py's own get_site_user call for why): confirmed
    empirically that resource_view_create/update fire the same
    activity-tracking signal resource_create does, which raises
    ValidationError({'user_id': ['Not found: User']}) with plain
    ignore_auth and no real context['user'] to attribute the change
    to - same root cause, same fix, as that one.

    Never fatal: a Link resource that's merely missing (or has a
    stale) image preview is a cosmetic gap, not a reason to fail the
    publish/republish job that called this."""
    if not content_url or not _is_image_format(fmt):
        return

    context = {'ignore_auth': True, 'user': user_name}
    try:
        views = toolkit.get_action('resource_view_list')(context, {'id': resource_id})
        image_views = [v for v in views if v.get('view_type') == 'image_view']
        if image_views:
            view = image_views[0]
            if view.get('image_url') == content_url:
                return
            toolkit.get_action('resource_view_update')(
                context,
                {'id': view['id'], 'image_url': content_url},
            )
        else:
            toolkit.get_action('resource_view_create')(
                context,
                {
                    'resource_id': resource_id,
                    'view_type': 'image_view',
                    'title': toolkit._('Image'),
                    'image_url': content_url,
                },
            )
    except toolkit.ObjectNotFound:
        pass
    except Exception:
        log.exception(
            'image_preview: failed to sync the Image view for resource %s',
            resource_id,
        )
