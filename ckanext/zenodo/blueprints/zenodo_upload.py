"""Receives a multipart file upload from the browser - the same XHR-with-
progress pattern ckanext-fair3r's resource-upload.js already uses for
/api/3/action/resource_create (see createOneResource there) - pointed at
this endpoint instead for files that belong on Zenodo, and streams the
file to the dataset's Zenodo deposition bucket.

A Flask blueprint, not a CKAN Action: the browser needs a raw multipart
stream with upload-progress events, and CKAN's action dispatch
(ckan.logic) fully deserializes the request body before a function ever
sees it - there's no supported way to get a streaming file handle out of
that layer. See the implementation plan's "Pourquoi un nouveau blueprint
Flask, pas une Action CKAN" section.

Security, in the order checked (never trust the client's own earlier
size/format decision for anything):
1. Login required, then CKAN's own resource_create authorization for the
   target dataset - the same permission already required for any normal
   resource add, so no new attacker surface.
2. Redis-backed rate limits (request count AND byte volume - see
   lib/rate_limit.py for why both matter), checked before accepting any
   of the file's bytes.
3. Redis-backed disk-space reservation (lib/disk_guard.py) for the
   declared body size, checked and reserved before the body is ever
   read - rate_limit.py's budgets are per-user and don't stop several
   different users from exhausting the server's disk at the same time.
4. The file's real size/format re-derived server-side. Only a file that's
   genuinely too big or an unreadable format for CKAN storage is
   accepted here, unless the user explicitly ticked "force Zenodo".
"""

import logging

from ckan.common import current_user
from ckan.model import repo
from ckan.plugins import toolkit
from flask import Blueprint, jsonify, request

from ckanext.zenodo.lib import (
    chunked_upload,
    delete_zenodo_file,
    disk_guard,
    rate_limit,
    record_limits,
    zenodo_client,
    zenodo_config,
)
from ckanext.zenodo.lib.publish_guard import maybe_publish_pending_deposition
from ckanext.zenodo.model import ZenodoDepositionQuery, ZenodoUploadQuery
from ckanext.zenodo.model import zenodo_upload as upload_states

log = logging.getLogger(__name__)

zenodo_upload = Blueprint('zenodo_upload', __name__)


def _error(message, status=400):
    return jsonify({'success': False, 'error': {'message': message}}), status


NOT_CONFIGURED = "Zenodo isn't set up on this site - ask its administrators."


def _file_size(file_storage):
    stream = file_storage.stream
    stream.seek(0, 2)  # SEEK_END
    size = stream.tell()
    stream.seek(0)
    return size


def _is_xloader_readable(filename, mimetype):
    formats = [f.lower() for f in zenodo_config.xloader_formats()]
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    mimetype = (mimetype or '').lower()
    return ext in formats or mimetype in formats


def _file_rule_error(filename, mimetype, size, forced):
    """Why this file can't go to Zenodo, or None. The same rules for the
    single-request and the chunked upload, re-derived on the server
    whatever the browser decided."""
    if size <= 0:
        return 'Empty file'
    if size > zenodo_config.max_file_size_bytes():
        return 'File too large'
    if not forced:
        fits_in_ckan = size <= zenodo_config.ckan_max_resource_size_bytes()
        if fits_in_ckan and _is_xloader_readable(filename, mimetype):
            return (
                'This file is small enough and a readable format for '
                'CKAN storage - use the normal upload instead, or tick '
                '"force Zenodo" if you want it there anyway.'
            )
    return None


def _check_record_limits(package_id, new_size):
    """Zenodo's own per-record limits (total size, file count) against the
    deposition's *real* current files, fetched live from Zenodo rather
    than tracked locally: files get deleted and re-versioned through
    several code paths (lib/delete_zenodo_file.py, the new-version flow
    below), so a locally-kept running total would drift. Raises
    RecordLimitExceeded, or ZenodoError if Zenodo can't be reached."""
    deposition = ZenodoDepositionQuery.read_package(package_id)
    existing = []
    if deposition is not None:
        token = zenodo_config.access_token(deposition.is_sandbox)
        data = zenodo_client.get_deposition(
            deposition.zenodo_deposition_id, token, sandbox=deposition.is_sandbox
        )
        existing = data.get('files') or []
    record_limits.check(
        existing,
        new_size,
        zenodo_config.max_record_bytes(),
        zenodo_config.max_files_per_record(),
    )


@zenodo_upload.route('/zenodo/upload/<package_name>', methods=['POST'])
def upload(package_name):
    if current_user.is_anonymous:
        return _error('Login required', 403)
    if not zenodo_config.is_configured():
        return _error(NOT_CONFIGURED, 503)

    context = {'user': current_user.name}
    try:
        pkg_dict = toolkit.get_action('package_show')(context, {'id': package_name})
    except toolkit.ObjectNotFound:
        return _error('Dataset not found', 404)

    try:
        toolkit.check_access('resource_create', context, {'package_id': pkg_dict['id']})
    except toolkit.NotAuthorized:
        return _error('Not authorized to add a resource to this dataset', 403)

    user_id = current_user.id

    try:
        rate_limit.check_and_increment_request(
            user_id, zenodo_config.max_uploads_per_hour()
        )
        rate_limit.check_bytes_budget(user_id, zenodo_config.max_bytes_per_hour())
    except rate_limit.RateLimitExceeded as e:
        return _error(str(e), 429)

    # Reserve disk headroom for the declared body size *before* touching
    # request.files just below - that first access is what makes
    # Werkzeug spool the whole multipart body to local disk (see
    # disk_guard.py's own docstring), so this is the last point where
    # rejecting outright still avoids writing anything at all. The
    # browser always sets Content-Length for a FormData body (it's never
    # chunked for a plain XHR.send()), so this is the real upcoming size,
    # not an estimate.
    declared_size = request.content_length or 0
    if declared_size <= 0:
        return _error('Missing or invalid upload size')

    # Checked here, before reserving or reading any of the body, so a
    # file that can never fit in this record is refused without spooling
    # it to disk first. declared_size is the whole multipart body, a few
    # hundred bytes above the file itself - a deliberately conservative
    # bound that can only matter within that margin of the limit.
    try:
        _check_record_limits(pkg_dict['id'], declared_size)
    except record_limits.RecordLimitExceeded as e:
        return _error(str(e), 409)
    except zenodo_client.ZenodoError:
        log.exception(
            'Failed to read the current Zenodo files for package %s', pkg_dict['id']
        )
        return _error('Could not reach Zenodo - try again shortly', 502)

    try:
        reserved = disk_guard.reserve(declared_size)
    except disk_guard.DiskSpaceExceeded as e:
        return _error(str(e), 507)

    try:
        uploaded = request.files.get('upload')
        if uploaded is None or not uploaded.filename:
            return _error('No file provided')

        forced = toolkit.asbool(request.form.get('force_zenodo', False))

        size = _file_size(uploaded)
        rule_error = _file_rule_error(
            uploaded.filename, uploaded.mimetype, size, forced
        )
        if rule_error:
            return _error(rule_error)

        try:
            rate_limit.add_bytes(user_id, size, zenodo_config.max_bytes_per_hour())
        except rate_limit.RateLimitExceeded as e:
            return _error(str(e), 429)

        sandbox = zenodo_config.sandbox_mode()
        token = zenodo_config.access_token(sandbox)

        def create_deposition_fn():
            deposition = zenodo_client.create_deposition(token, sandbox=sandbox)
            return deposition['id'], deposition['links']['bucket']

        try:
            deposition = ZenodoDepositionQuery.get_or_create_open_deposition(
                pkg_dict['id'],
                create_deposition_fn,
                is_sandbox=sandbox,
                discard_fn=lambda deposition_id: zenodo_client.delete_deposition(
                    deposition_id, token, sandbox=sandbox
                ),
            )
        except zenodo_client.ZenodoError:
            log.exception(
                'Failed to create/fetch Zenodo deposition for package %s',
                pkg_dict['id'],
            )
            return _error('Could not reach Zenodo - try again shortly', 502)

        try:
            bucket_url, new_draft = chunked_upload.bucket_for_upload(
                deposition, token, sandbox
            )
        except zenodo_client.ZenodoError:
            log.exception(
                'Failed to create a new Zenodo version for package %s - '
                'its deposition is already published',
                pkg_dict['id'],
            )
            return _error('Could not reach Zenodo - try again shortly', 502)

        try:
            zenodo_client.upload_file_to_bucket(
                bucket_url, uploaded.filename, uploaded.stream, token
            )
        except zenodo_client.ZenodoError:
            log.exception(
                'Zenodo upload failed for package %s, file %s',
                pkg_dict['id'],
                uploaded.filename,
            )
            return _error('Upload to Zenodo failed - try again shortly', 502)

        if new_draft is not None:
            # The upload above landed in a *new draft version*'s bucket,
            # not the already-published deposition's own one - see
            # chunked_upload.bucket_for_upload's own docstring. Only now, after that
            # upload actually succeeded, point this package's row at the
            # new draft and mark it unpublished again, so this file
            # isn't silently lost on an abandoned draft if anything above
            # had failed instead.
            ZenodoDepositionQuery.update_package(
                pkg_dict['id'],
                zenodo_deposition_id=new_draft['id'],
                bucket_url=bucket_url,
                published=False,
            )
        ZenodoDepositionQuery.add_file(pkg_dict['id'], uploaded.filename)
        # This is a raw Flask view, not a CKAN action - nothing else commits
        # for us (a CKAN action like package_create/resource_create commits
        # internally as part of its own implementation; ZenodoDepositionQuery
        # only flushes, matching DOIQuery's own pattern, which works for doi
        # only because every call site runs inside *some* action's own
        # request-response cycle). Confirmed directly: without this, a
        # deposition row created in one request was invisible to the next.
        repo.commit_and_remove()

        if new_draft is not None:
            # The dataset itself is already active + public (that's
            # exactly why its deposition was already published) - this
            # new draft needs its own publish to actually surface the
            # file, which won't happen on its own the way the very first
            # publish does (no package_update/after_dataset_update fires
            # from this raw Flask view). Enqueues the same deferred job
            # publish_guard.py's own hook uses - mints no second DOI
            # (same conceptrecid, just a new version of it), re-requests
            # community inclusion, and creates the new file's own CKAN
            # Link resource, all through the one already-tested code
            # path rather than duplicating any of it here.
            maybe_publish_pending_deposition(pkg_dict)

        return jsonify(
            {
                'success': True,
                'result': {
                    'filename': uploaded.filename,
                    'zenodo_deposition_id': new_draft['id']
                    if new_draft is not None
                    else deposition.zenodo_deposition_id,
                },
            }
        )
    finally:
        # Always releases, success or any failure/exception above -
        # Werkzeug has already deleted its own spooled temp file by the
        # time a raw Flask view like this one returns, so the disk is
        # genuinely free again at this point, not just logically so.
        disk_guard.release(reserved)


@zenodo_upload.route('/zenodo/upload-capacity', methods=['GET'])
def upload_capacity():
    """How large a file could actually be accepted right now, for the
    upload form's live capacity badge (resource-upload.js). Capped by
    max_file_size_bytes() - disk_guard.available_bytes() alone is raw
    disk headroom, which on a dev machine with hundreds of GB free would
    otherwise advertise far more than this plugin will ever actually
    accept (it enforces that same cap itself in upload() above,
    regardless of how much disk is free). Advisory only either way -
    available space can change between this call and the user's actual
    upload, so the real, authoritative check is still
    disk_guard.reserve() in upload() above; this endpoint never reserves
    anything itself."""
    if current_user.is_anonymous:
        return _error('Login required', 403)
    if not zenodo_config.is_configured():
        return _error(NOT_CONFIGURED, 503)

    available = min(disk_guard.available_bytes(), zenodo_config.max_file_size_bytes())
    return jsonify({'success': True, 'result': {'available_bytes': available}})


@zenodo_upload.route('/zenodo/upload/<package_name>/<filename>', methods=['DELETE'])
def delete_pending(package_name, filename):
    """Removes one file the user uploaded to Zenodo before the dataset
    (and so the deposition) was published - they're the user's own
    files, theirs to remove while there's still time. Once published,
    deleting a file goes through CKAN's own normal "delete resource"
    button instead (see lib/delete_zenodo_file.py's own docstring for
    why that's a different, deferred code path)."""
    if current_user.is_anonymous:
        return _error('Login required', 403)

    context = {'user': current_user.name}
    try:
        pkg_dict = toolkit.get_action('package_show')(context, {'id': package_name})
    except toolkit.ObjectNotFound:
        return _error('Dataset not found', 404)

    try:
        toolkit.check_access('package_update', context, {'id': pkg_dict['id']})
    except toolkit.NotAuthorized:
        return _error('Not authorized to edit this dataset', 403)

    try:
        deleted = delete_zenodo_file.delete_pending_file(pkg_dict['id'], filename)
    except zenodo_client.ZenodoError:
        log.exception(
            'Failed to delete %s from the pending Zenodo deposition for package %s',
            filename,
            pkg_dict['id'],
        )
        return _error('Could not delete the file from Zenodo - try again shortly', 502)

    if not deleted:
        return _error('Nothing pending to delete for that file', 404)

    return jsonify({'success': True})


# Chunked upload (lib/chunked_upload.py): short requests only, then a
# background job sends the file to Zenodo. Under /zenodo/uploads/, so
# nginx's normal limits (20M body, default timeouts) apply - every chunk
# fits them.


def _package(package_name, permission):
    """(pkg_dict, None), or (None, error response)."""
    if current_user.is_anonymous:
        return None, _error('Login required', 403)
    context = {'user': current_user.name}
    try:
        pkg_dict = toolkit.get_action('package_show')(context, {'id': package_name})
        data_dict = (
            {'package_id': pkg_dict['id']}
            if permission == 'resource_create'
            else {'id': pkg_dict['id']}
        )
        toolkit.check_access(permission, context, data_dict)
    except toolkit.ObjectNotFound:
        return None, _error('Dataset not found', 404)
    except toolkit.NotAuthorized:
        return None, _error('Not authorized for this dataset', 403)
    return pkg_dict, None


def _own_upload(pkg_dict, upload_id):
    """The upload, if it's this dataset's and this user's, else None."""
    upload = ZenodoUploadQuery.get(upload_id)
    if (
        upload is None
        or upload.package_id != pkg_dict['id']
        or upload.user_id != current_user.id
    ):
        return None
    return upload


def _upload_json(upload):
    response = jsonify(
        {
            'success': True,
            'result': {
                'upload_id': upload.id,
                'filename': upload.filename,
                'size': upload.size,
                'received': upload.received,
                'status': upload.status,
                'error': toolkit._(upload.error) if upload.error else None,
                'chunk_size': chunked_upload.CHUNK_SIZE,
            },
        }
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@zenodo_upload.route('/zenodo/uploads/<package_name>', methods=['POST'])
def chunked_start(package_name):
    """Every check of upload() above, once, for the whole file announced
    as JSON {filename, size, mimetype, force_zenodo}; reserves its disk
    space until the file is on Zenodo or the upload is dropped."""
    if not zenodo_config.is_configured():
        return _error(NOT_CONFIGURED, 503)
    pkg_dict, error = _package(package_name, 'resource_create')
    if error:
        return error

    body = request.get_json(silent=True) or {}
    filename = str(body.get('filename') or '').strip()
    try:
        size = int(body.get('size'))
    except (TypeError, ValueError):
        return _error('Missing or invalid upload size')
    if not filename or '/' in filename or '\\' in filename:
        return _error('No file provided')
    forced = toolkit.asbool(body.get('force_zenodo', False))

    user_id = current_user.id
    try:
        rate_limit.check_and_increment_request(
            user_id, zenodo_config.max_uploads_per_hour()
        )
        rate_limit.check_bytes_budget(user_id, zenodo_config.max_bytes_per_hour())
    except rate_limit.RateLimitExceeded as e:
        return _error(str(e), 429)

    rule_error = _file_rule_error(filename, body.get('mimetype'), size, forced)
    if rule_error:
        return _error(rule_error)

    try:
        _check_record_limits(pkg_dict['id'], size)
    except record_limits.RecordLimitExceeded as e:
        return _error(str(e), 409)
    except zenodo_client.ZenodoError:
        log.exception(
            'Failed to read the current Zenodo files for package %s', pkg_dict['id']
        )
        return _error('Could not reach Zenodo - try again shortly', 502)

    try:
        rate_limit.add_bytes(user_id, size, zenodo_config.max_bytes_per_hour())
    except rate_limit.RateLimitExceeded as e:
        return _error(str(e), 429)

    try:
        disk_guard.reserve(size)
    except disk_guard.DiskSpaceExceeded as e:
        return _error(str(e), 507)
    try:
        upload = ZenodoUploadQuery.create(
            pkg_dict['id'], user_id, filename, size, forced
        )
        repo.commit()
    except Exception:
        disk_guard.release(size)
        raise
    return _upload_json(upload)


@zenodo_upload.route(
    '/zenodo/uploads/<package_name>/<upload_id>/chunk', methods=['PUT']
)
def chunked_chunk(package_name, upload_id):
    """One chunk, as the raw request body, at ?offset=. Sending the chunk
    at the current received size again replaces it - what the browser does
    when an answer got lost."""
    pkg_dict, error = _package(package_name, 'resource_create')
    if error:
        return error
    upload = _own_upload(pkg_dict, upload_id)
    if upload is None:
        return _error('Upload not found', 404)
    if upload.status != upload_states.RECEIVING:
        return _error('This upload is no longer receiving chunks', 409)

    try:
        offset = int(request.args.get('offset', ''))
    except ValueError:
        return _error('Missing or invalid offset')
    length = request.content_length or 0
    if (
        offset < 0
        or offset > upload.received
        or length <= 0
        or length > chunked_upload.CHUNK_SIZE
        or offset + length > upload.size
    ):
        return _error('Chunk out of range', 416)

    try:
        received = chunked_upload.write_chunk(upload, offset, request.stream, length)
    except ValueError:
        return _error('Incomplete chunk - send it again')
    ZenodoUploadQuery.update(upload, received=received)
    repo.commit()
    return _upload_json(upload)


@zenodo_upload.route(
    '/zenodo/uploads/<package_name>/<upload_id>/finish', methods=['POST']
)
def chunked_finish(package_name, upload_id):
    """All chunks in: the background job takes over the send to Zenodo."""
    pkg_dict, error = _package(package_name, 'resource_create')
    if error:
        return error
    upload = _own_upload(pkg_dict, upload_id)
    if upload is None:
        return _error('Upload not found', 404)
    if upload.status != upload_states.RECEIVING:
        return _upload_json(upload)
    if upload.received != upload.size:
        return _error('Some chunks are missing', 409)
    chunked_upload.enqueue_send(upload)
    return _upload_json(upload)


@zenodo_upload.route('/zenodo/uploads/<package_name>/<upload_id>', methods=['GET'])
def chunked_status(package_name, upload_id):
    pkg_dict, error = _package(package_name, 'resource_create')
    if error:
        return error
    upload = _own_upload(pkg_dict, upload_id)
    if upload is None:
        # Gone: on Zenodo now (or cancelled).
        return _error('Upload not found', 404)
    return _upload_json(upload)


@zenodo_upload.route(
    '/zenodo/uploads/<package_name>/<upload_id>/retry', methods=['POST']
)
def chunked_retry(package_name, upload_id):
    """A failed send, sent again - by any editor of the dataset."""
    pkg_dict, error = _package(package_name, 'package_update')
    if error:
        return error
    upload = ZenodoUploadQuery.get(upload_id)
    if upload is None or upload.package_id != pkg_dict['id']:
        return _error('Upload not found', 404)
    if upload.status != upload_states.FAILED:
        return _error('Only a failed upload can be retried', 409)
    chunked_upload.enqueue_send(upload)
    return _upload_json(upload)


@zenodo_upload.route('/zenodo/uploads/<package_name>/<upload_id>', methods=['DELETE'])
def chunked_cancel(package_name, upload_id):
    """Drops an upload not yet on Zenodo (its file and disk reservation) -
    by the user sending it, or any editor of the dataset. Not while the
    job is sending it."""
    if current_user.is_anonymous:
        return _error('Login required', 403)
    pkg_dict, error = _package(package_name, 'resource_create')
    if error:
        return error
    upload = ZenodoUploadQuery.get(upload_id)
    if upload is None or upload.package_id != pkg_dict['id']:
        return _error('Upload not found', 404)
    if upload.user_id != current_user.id:
        _, error = _package(package_name, 'package_update')
        if error:
            return error
    if upload.status == upload_states.SENDING:
        return _error('Being sent to Zenodo right now - try again shortly', 409)
    chunked_upload.discard(upload)
    repo.commit()
    return jsonify({'success': True})
