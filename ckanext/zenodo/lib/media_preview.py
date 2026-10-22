"""Keeps a Zenodo-backed resource's own core media view (Image, Audio
or Video) pointing at a real, directly-loadable content URL -
instead of the broken one CKAN auto-creates by default.

Found via live testing (the exact scenario the user asked about):
publish_guard._create_zenodo_resources sets `format` on each resource
(needed for the right icon - see publish_guard.py's own docstring),
and CKAN's own default-view auto-creation then adds a matching view
for anything whose format matches one of these plugins' own
`ckan.preview.*_formats` - pointing it at `resource.url` by default,
which here is Zenodo's own HTML landing page (see
_zenodo_file_preview_url), not the actual file bytes. The result:
whatever tag that view's own template renders (<img>, <audio>,
<video>, <object>) just shows nothing usable.

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
which <img>, <audio> and <video> load as-is. Not PDFs: the file comes
as an attachment with X-Frame-Options: sameorigin, which pdf_view's
<object> respects - see _NO_PREVIEW below. Each of core's own
image_view, audio_view and video_view already supports pointing at a
URL other than resource.url for exactly this, via its own schema field
(image_url/audio_url/video_url) - this module just keeps the
right one in sync, rather than reinventing new resource view types.
No new route, no new auth surface, no content ever executed - a
single config field, filled from a URL this plugin already built
itself (quote()-escaped the same way as everywhere else in this
codebase), on views that are entirely CKAN core's own code.

That per-file content link is tied to one specific *version*'s own
record id, not the stable concept id, and does go stale across a
later version (e.g. after deleting a different file - see
delete_zenodo_file.py). Both publish_guard.py (first publish) and
delete_zenodo_file.py (republish after a delete) call
sync_media_preview with each publish's own fresh record id, so this
self-heals on every (re)publish rather than needing to stay correct
forever on its own.
"""

import logging
from urllib.parse import quote

from ckan.plugins import toolkit

log = logging.getLogger(__name__)

# One entry per core view this applies to - (view_type, its own schema
# field for the override URL, the config option listing which formats
# it covers).
_VIEW_KINDS = (
    ('image_view', 'image_url', 'ckan.preview.image_formats', 'Image'),
    ('audio_view', 'audio_url', 'ckan.preview.audio_formats', 'Audio'),
    ('video_view', 'video_url', 'ckan.preview.video_formats', 'Video'),
)

# No PDF preview: Zenodo serves every file as a download that can't be
# embedded (application/octet-stream, Content-Disposition: attachment,
# X-Frame-Options: sameorigin - checked on the sandbox), and pdf_view's
# <object> respects that: only a blank box shows. CKAN creates a pdf_view
# for any PDF resource by default, so it's removed instead; "Go to
# resource" opens the file in Zenodo's own preview.
_NO_PREVIEW = {'pdf': 'pdf_view', 'pdf/a': 'pdf_view'}


def zenodo_file_content_url(record_id, sandbox, filename):
    """The *version-specific* record's own direct file content link -
    deliberately not using whatever `links.download` the legacy
    deposit API's own `files` list reports: confirmed empirically that
    it's stale/wrong even right after a real publish (still pointing
    at a `.../draft/files/...` path that 404s). Built directly instead,
    matching the one confirmed working via a real sandbox request."""
    host = 'sandbox.zenodo.org' if sandbox else 'zenodo.org'
    return f'https://{host}/api/records/{record_id}/files/{quote(filename)}/content'


def _view_kind_for_format(fmt):
    fmt = (fmt or '').lower()
    for view_type, url_field, config_key, title in _VIEW_KINDS:
        formats = {f.lower() for f in (toolkit.config.get(config_key) or '').split()}
        if fmt in formats:
            return view_type, url_field, title
    return None


def sync_media_preview(resource_id, fmt, content_url, user_name):
    """Ensures exactly one view of the right kind (Image/Audio/Video,
    picked from `fmt`) on this resource points at `content_url` -
    updates CKAN's own auto-created one if present, creates one
    otherwise (defensive - core's default-view creation should already
    have made one for anything with a matching format). No-op if `fmt`
    doesn't match any of these, or if content_url isn't known yet.
    A PDF's own view is deleted instead (see _NO_PREVIEW).

    `user_name` must be a real user (e.g. the site user - see
    publish_guard.py's own get_site_user call for why): confirmed
    empirically that resource_view_create/update fire the same
    activity-tracking signal resource_create does, which raises
    ValidationError({'user_id': ['Not found: User']}) with plain
    ignore_auth and no real context['user'] to attribute the change
    to - same root cause, same fix, as that one.

    Never fatal: a Link resource that's merely missing (or has a
    stale) preview is a cosmetic gap, not a reason to fail the
    publish/republish job that called this."""
    if not content_url:
        return
    unusable_view = _NO_PREVIEW.get((fmt or '').lower())
    if unusable_view:
        _delete_views(resource_id, unusable_view, user_name)
        return
    kind = _view_kind_for_format(fmt)
    if kind is None:
        return
    view_type, url_field, title = kind

    context = {'ignore_auth': True, 'user': user_name}
    try:
        views = toolkit.get_action('resource_view_list')(context, {'id': resource_id})
        matching = [v for v in views if v.get('view_type') == view_type]
        if matching:
            view = matching[0]
            if view.get(url_field) == content_url:
                return
            toolkit.get_action('resource_view_update')(
                context,
                {'id': view['id'], url_field: content_url},
            )
        else:
            toolkit.get_action('resource_view_create')(
                context,
                {
                    'resource_id': resource_id,
                    'view_type': view_type,
                    'title': toolkit._(title),
                    url_field: content_url,
                },
            )
    except toolkit.ObjectNotFound:
        pass
    except Exception:
        log.exception(
            'media_preview: failed to sync the %s view for resource %s',
            view_type,
            resource_id,
        )


def _delete_views(resource_id, view_type, user_name):
    context = {'ignore_auth': True, 'user': user_name}
    try:
        for view in toolkit.get_action('resource_view_list')(
            context, {'id': resource_id}
        ):
            if view.get('view_type') == view_type:
                toolkit.get_action('resource_view_delete')(context, {'id': view['id']})
    except toolkit.ObjectNotFound:
        pass
    except Exception:
        log.exception(
            'media_preview: failed to remove the %s view of resource %s',
            view_type,
            resource_id,
        )
