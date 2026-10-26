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
and they can retry or cancel it themselves - even while it's being sent:
the job stops at its next check. Abandoned uploads are cleaned by the
same command.

The file is kept out of the kernel's page cache (written and read once):
on a 2 GB VM, a multi-GB file there pushed CKAN, Postgres and Solr into
swap and slowed the whole site (measured on validation).
"""

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from ckan.lib.redis import connect_to_redis
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
# How often the send job drops what it read from the page cache and looks
# for a cancel.
CHECK_EVERY = 64 * 1024 * 1024
CANCEL_KEY = 'zenodo:upload:cancel:{}'


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


def _drop_from_cache(fd):
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


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
        handle.flush()
        # Dirty pages can't be dropped: on disk first.
        os.fdatasync(handle.fileno())
        _drop_from_cache(handle.fileno())
    return offset + length


class SendCancelled(Exception):
    """Raised by _SendReader when the upload was cancelled mid-send."""


def request_cancel(upload_id):
    """Asks the job sending this upload to stop. Kept a day: a job that died
    mid-send is only retried hours later, and must see it then too."""
    connect_to_redis().set(
        CANCEL_KEY.format(upload_id), 1, ex=int(KEEP_FAILED_FOR.total_seconds())
    )


def cancel_requested(upload_id):
    return bool(connect_to_redis().exists(CANCEL_KEY.format(upload_id)))


class _SendReader:
    """The assembled file as requests reads it for the PUT, dropping what
    was read from the page cache and checking for a cancel every
    CHECK_EVERY bytes. requests sizes the body with __len__ and tell (no
    fileno: requests would then read a `mode` this object doesn't have),
    and zenodo_client rewinds it with seek before a retry."""

    def __init__(self, handle, upload_id):
        self._handle = handle
        self._upload_id = upload_id
        self._unchecked = 0

    def __len__(self):
        return os.fstat(self._handle.fileno()).st_size

    def tell(self):
        return self._handle.tell()

    def seek(self, *args):
        return self._handle.seek(*args)

    def read(self, size=-1):
        block = self._handle.read(size)
        self._unchecked += len(block)
        if self._unchecked >= CHECK_EVERY:
            self._unchecked = 0
            _drop_from_cache(self._handle.fileno())
            if cancel_requested(self._upload_id):
                raise SendCancelled()
        return block


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


def _drop_local(upload_id, size):
    try:
        os.remove(os.path.join(upload_dir(), upload_id))
    except FileNotFoundError:
        pass
    disk_guard.release(size)
    connect_to_redis().delete(CANCEL_KEY.format(upload_id))


def discard(upload):
    """Removes an upload for good: its row, then its file and disk
    reservation - only by whoever removed the row, so they're never
    released twice."""
    if ZenodoUploadQuery.delete(upload):
        _drop_local(upload.id, upload.size)


def forget_purged(uploads):
    """After dataset_purge, for the dataset's uploads read before it as
    (id, size, status): their rows went with the package (ON DELETE
    CASCADE), their files and disk reservations didn't. A job still
    sending one is also told to stop."""
    for upload_id, size, status in uploads:
        _drop_local(upload_id, size)
        if status == upload_states.SENDING:
            request_cancel(upload_id)


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
    if cancel_requested(upload_id):
        # Cancelled while a previous attempt was sending it.
        discard(upload)
        repo.commit()
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
                bucket_url, upload.filename, _SendReader(handle, upload.id), token
            )
    except SendCancelled:
        # An interrupted PUT leaves nothing in the bucket, not even over a
        # file of the same name (checked on the sandbox).
        log.info('Sending %s to Zenodo cancelled', upload.filename)
        discard(upload)
        repo.commit()
        return
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
