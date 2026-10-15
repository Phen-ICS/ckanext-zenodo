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
3. The file's real size/format re-derived server-side. Only a file that's
   genuinely too big or an unreadable format for CKAN storage is
   accepted here, unless the user explicitly ticked "force Zenodo".
"""

import logging

from ckan.common import current_user
from ckan.model import repo
from ckan.plugins import toolkit
from flask import Blueprint, jsonify, request

from ckanext.zenodo.lib import rate_limit, zenodo_client, zenodo_config
from ckanext.zenodo.model import ZenodoDepositionQuery

log = logging.getLogger(__name__)

zenodo_upload = Blueprint('zenodo_upload', __name__)


def _error(message, status=400):
    return jsonify({'success': False, 'error': {'message': message}}), status


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


@zenodo_upload.route('/zenodo/upload/<package_name>', methods=['POST'])
def upload(package_name):
    if current_user.is_anonymous:
        return _error('Login required', 403)

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

    uploaded = request.files.get('upload')
    if uploaded is None or not uploaded.filename:
        return _error('No file provided')

    forced = toolkit.asbool(request.form.get('force_zenodo', False))

    size = _file_size(uploaded)
    if size <= 0:
        return _error('Empty file')
    if size > zenodo_config.max_file_size_bytes():
        return _error('File too large')

    if not forced:
        fits_in_ckan = size <= zenodo_config.ckan_max_resource_size_bytes()
        readable = _is_xloader_readable(uploaded.filename, uploaded.mimetype)
        if fits_in_ckan and readable:
            return _error(
                'This file is small enough and a readable format for '
                'CKAN storage - use the normal upload instead, or tick '
                '"force Zenodo" if you want it there anyway.'
            )

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
            pkg_dict['id'], create_deposition_fn, is_sandbox=sandbox
        )
    except zenodo_client.ZenodoError:
        log.exception(
            'Failed to create/fetch Zenodo deposition for package %s', pkg_dict['id']
        )
        return _error('Could not reach Zenodo - try again shortly', 502)

    try:
        zenodo_client.upload_file_to_bucket(
            deposition.bucket_url, uploaded.filename, uploaded.stream, token
        )
    except zenodo_client.ZenodoError:
        log.exception(
            'Zenodo upload failed for package %s, file %s',
            pkg_dict['id'],
            uploaded.filename,
        )
        return _error('Upload to Zenodo failed - try again shortly', 502)

    ZenodoDepositionQuery.add_file(pkg_dict['id'], uploaded.filename)
    # This is a raw Flask view, not a CKAN action - nothing else commits
    # for us (a CKAN action like package_create/resource_create commits
    # internally as part of its own implementation; ZenodoDepositionQuery
    # only flushes, matching DOIQuery's own pattern, which works for doi
    # only because every call site runs inside *some* action's own
    # request-response cycle). Confirmed directly: without this, a
    # deposition row created in one request was invisible to the next.
    repo.commit_and_remove()

    return jsonify(
        {
            'success': True,
            'result': {
                'filename': uploaded.filename,
                'zenodo_deposition_id': deposition.zenodo_deposition_id,
            },
        }
    )
