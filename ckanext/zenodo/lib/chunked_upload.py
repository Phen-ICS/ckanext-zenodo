"""Uploads in chunks, then a background job sends the file to Zenodo.

Why: the web server runs one uWSGI process on 1-2 GB VMs, and uWSGI kills
any request over 50 s (harakiri). A multi-GB file sent in one request
(the original /zenodo/upload/<dataset> route) can't finish, and holds the
site while it tries. Here every request is short:

1. start: all the checks of a normal upload (permission, rate limits,
   size/format, Zenodo's record limits), and the disk space is reserved;
2. chunks of CHUNK_SIZE bytes, each its own request, written to a file
   under ckan.storage_path - a lost chunk is just sent again;
3. finish: a background job (the CKAN worker) sends the whole file to the
   dataset's Zenodo deposition, reading it from disk as it goes.

A failed send keeps the file: it's retried by `ckan zenodo retry-pending`
(cron) up to MAX_SEND_ATTEMPTS times, the dataset page tells its editors,
and they can retry or cancel it themselves. Abandoned uploads are
cleaned by the same command.
"""

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from ckan.model import repo
from ckan.plugins import toolkit

from ckanext.zenodo.lib import disk_guard, zenodo_client, zenodo_config
from ckanext.zenodo.lib.publish_guard import maybe_publish_pending_deposition
from ckanext.zenodo.model import ZenodoDepositionQuery, ZenodoUploadQuery
from ckanext.zenodo.model import zenodo_upload as upload_states

log = logging.getLogger(__name__)

# Under nginx's 20M client_max_body_size for the whole site, so chunks need
# no special route and pass any proxy that lets normal CKAN uploads through.
CHUNK_SIZE = 16 * 1024 * 1024
# The send job reads a file of up to max_file_size_bytes (10 GB) and
# streams it to Zenodo; CKAN's default job timeout (180 s) would kill it.
SEND_TIMEOUT_SECONDS = 6 * 3600
MAX_SEND_ATTEMPTS = 3
RECEIVING_ABANDONED_AFTER = timedelta(hours=2)
RETRY_FAILED_AFTER = timedelta(minutes=30)
KEEP_FAILED_FOR = timedelta(hours=24)


def N_(message):
    """Marked for extraction, translated when shown (stored in English on
    the row) - same as helpers.N_, which can't be imported from here."""
    return message


SEND_FAILED = N_('Zenodo could not be reached or refused the file.')
SEND_INTERRUPTED = N_('The transfer to Zenodo was interrupted.')
NOT_CONFIGURED = N_("Zenodo isn't set up on this site any more.")


def upload_dir():
    """Where chunks are assembled: under ckan.storage_path (persistent,
    readable by the web process and the worker alike), else the temp
    directory."""
    storage = toolkit.config.get('ckan.storage_path')
    path = (
        os.path.join(storage, 'zenodo_uploads')
        if storage
        else os.path.join(tempfile.gettempdir(), 'ckanext-zenodo-uploads')
    )
    os.makedirs(path, exist_ok=True)
    return path


def file_path(upload):
    return os.path.join(upload_dir(), upload.id)


def write_chunk(upload, offset, stream, length):
    """Writes `length` bytes from `stream` at `offset`, dropping anything
    after it (a chunk sent again after a lost answer replaces its first
    attempt). Returns the new received size. Raises ValueError if the
    stream ends early."""
    path = file_path(upload)
    with open(path, 'r+b' if os.path.exists(path) else 'wb') as handle:
        handle.truncate(offset)
        handle.seek(offset)
        remaining = length
        while remaining:
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError('Chunk shorter than announced')
            handle.write(block)
            remaining -= len(block)
    return offset + length


def bucket_for_upload(deposition, token, sandbox):
    """Where to PUT the next file: the deposition's own bucket while it's
    unpublished, else a *new draft version*'s bucket - a file written into
    a published record's bucket is accepted (200) but never appears on the
    record (confirmed on the sandbox). Returns (bucket_url, new_draft or
    None); the caller only records the new draft once the file is in it,
    so a failure leaves the published deposition untouched."""
    if not deposition.published:
        return deposition.bucket_url, None
    new_draft = zenodo_client.create_new_version(
        deposition.zenodo_deposition_id, token, sandbox=sandbox
    )
    return new_draft['links']['bucket'], new_draft


def discard(upload):
    """Removes an upload for good: its file, its disk reservation, its row."""
    try:
        os.remove(file_path(upload))
    except FileNotFoundError:
        pass
    disk_guard.release(upload.size)
    ZenodoUploadQuery.delete(upload)


def enqueue_send(upload):
    ZenodoUploadQuery.update(upload, status=upload_states.QUEUED, error=None)
    repo.commit()
    toolkit.enqueue_job(
        send_to_zenodo,
        kwargs={'upload_id': upload.id},
        title=f'zenodo: send {upload.filename} ({upload.package_id})',
        rq_kwargs={'timeout': SEND_TIMEOUT_SECONDS},
    )


def _fail(upload, message):
    ZenodoUploadQuery.update(upload, status=upload_states.FAILED, error=message)
    repo.commit()


def send_to_zenodo(upload_id):
    """RQ job: the assembled file into the dataset's deposition. Same steps
    as the single-request upload route did inline (deposition, new version
    if already published, PUT into the bucket), then the publish job if
    the dataset is public. Never raises: a failure is recorded on the row
    for the dataset page and the retry rules (module docstring)."""
    upload = ZenodoUploadQuery.get(upload_id)
    if upload is None or upload.status != upload_states.QUEUED:
        return
    ZenodoUploadQuery.update(
        upload,
        status=upload_states.SENDING,
        send_attempts=(upload.send_attempts or 0) + 1,
    )
    repo.commit()

    try:
        pkg_dict = toolkit.get_action('package_show')(
            {'ignore_auth': True}, {'id': upload.package_id}
        )
    except toolkit.ObjectNotFound:
        discard(upload)
        repo.commit()
        return

    sandbox = zenodo_config.sandbox_mode()
    if not zenodo_config.is_configured(sandbox):
        _fail(upload, NOT_CONFIGURED)
        return
    token = zenodo_config.access_token(sandbox)

    def create_deposition_fn():
        deposition = zenodo_client.create_deposition(token, sandbox=sandbox)
        return deposition['id'], deposition['links']['bucket']

    try:
        deposition = ZenodoDepositionQuery.get_or_create_open_deposition(
            upload.package_id,
            create_deposition_fn,
            is_sandbox=sandbox,
            discard_fn=lambda deposition_id: zenodo_client.delete_deposition(
                deposition_id, token, sandbox=sandbox
            ),
        )
        repo.commit()
        bucket_url, new_draft = bucket_for_upload(deposition, token, sandbox)
        with open(file_path(upload), 'rb') as handle:
            zenodo_client.upload_file_to_bucket(
                bucket_url, upload.filename, handle, token
            )
    except (zenodo_client.ZenodoError, OSError):
        log.exception(
            'Sending %s to Zenodo failed (package %s, attempt %s)',
            upload.filename,
            upload.package_id,
            upload.send_attempts,
        )
        _fail(upload, SEND_FAILED)
        return

    if new_draft is not None:
        ZenodoDepositionQuery.update_package(
            upload.package_id,
            zenodo_deposition_id=new_draft['id'],
            bucket_url=bucket_url,
            published=False,
        )
    ZenodoDepositionQuery.add_file(upload.package_id, upload.filename)
    discard(upload)
    repo.commit()
    log.info('Sent %s to Zenodo for package %s', upload.filename, pkg_dict['id'])

    # Publishes now if the dataset is already public (a new version, or a
    # first publish that waited for this file); no-op otherwise.
    maybe_publish_pending_deposition(pkg_dict)


def housekeeping(now=None):
    """Run by `ckan zenodo retry-pending`. Returns how many uploads were
    (abandoned, interrupted, retried, expired)."""
    now = now or datetime.now(timezone.utc)
    counts = {'abandoned': 0, 'interrupted': 0, 'retried': 0, 'expired': 0}

    def idle(upload):
        updated = upload.updated
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return now - updated

    for upload in ZenodoUploadQuery.all():
        if upload.status == upload_states.RECEIVING:
            if idle(upload) > RECEIVING_ABANDONED_AFTER:
                discard(upload)
                counts['abandoned'] += 1
        elif upload.status == upload_states.SENDING:
            # Past the job's own timeout: the worker died or was killed.
            if idle(upload) > timedelta(seconds=SEND_TIMEOUT_SECONDS):
                ZenodoUploadQuery.update(
                    upload, status=upload_states.FAILED, error=SEND_INTERRUPTED
                )
                counts['interrupted'] += 1
        elif upload.status == upload_states.FAILED:
            if (upload.send_attempts or 0) < MAX_SEND_ATTEMPTS:
                if idle(upload) > RETRY_FAILED_AFTER:
                    enqueue_send(upload)
                    counts['retried'] += 1
            elif idle(upload) > KEEP_FAILED_FOR:
                discard(upload)
                counts['expired'] += 1
    repo.commit()
    return counts
