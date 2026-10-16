"""Server-wide disk-space guard for the upload blueprint.

rate_limit.py's budgets are per-user: they bound how much *one* user can
upload per hour, but say nothing about several different users, each well
within their own budget, uploading large files at the very same moment.
Werkzeug spools the whole multipart body to local disk before this
plugin's blueprint ever runs (see zenodo_config.py's own note on
DEFAULT_MAX_FILE_SIZE_BYTES for why), so several concurrent large uploads
can fill the server's disk regardless of any per-user limit. This module
is what actually prevents that, with a single Redis counter shared by
every user and worker process.

reserve()/release() use plain INCRBY/DECRBY as the only atomic primitive,
not a check-then-set: two concurrent reserve() calls can never both pass
a check based on the same stale reading of the counter, because the
increment itself happens first and is what's checked. There's
deliberately no TTL on the shared counter itself - a TTL on a value that
several independent reservations share would expire the whole thing (and
silently wipe other still-active reservations) as soon as the first one's
TTL elapsed, not when it's actually released. Instead, every call site
releases in a `finally` block (see zenodo_upload.py), which this
deployment's Waitress server (a synchronous, one-thread-per-request WSGI
server - see ckan.ini.template's [server:main]) runs reliably even on a
client disconnect; `ckan zenodo disk-reservations` (cli.py) is the manual
escape hatch for the rare case a worker is killed before its `finally`
runs.
"""

import logging
import shutil
import tempfile

from ckan.lib.redis import connect_to_redis

from ckanext.zenodo.lib import zenodo_config

log = logging.getLogger(__name__)

RESERVED_KEY = 'zenodo:disk:reserved_bytes'


class DiskSpaceExceeded(Exception):
    """Raised by reserve() when the declared upload size doesn't fit in
    the currently available disk space (free space, minus the configured
    safety margin, minus every upload already reserved/in flight)."""


def disk_free_bytes():
    """Free space where Werkzeug actually spools the incoming multipart
    body - the OS/environment's own temp dir (TMPDIR), not anything
    configurable through CKAN. A thin wrapper around shutil.disk_usage
    only so tests have one obvious seam to patch."""
    return shutil.disk_usage(tempfile.gettempdir()).free


def reserved_bytes():
    redis = connect_to_redis()
    return int(redis.get(RESERVED_KEY) or 0)


def available_bytes():
    """Best-effort snapshot of how many bytes could currently be
    accepted: disk free space, minus the configured safety margin, minus
    every upload already reserved by this or any other worker. Advisory
    only, for display (the /zenodo/upload-capacity endpoint) - reserve()
    below is the real, authoritative check, done atomically at accept
    time, not from this same read."""
    free = (
        disk_free_bytes() - zenodo_config.disk_safety_margin_bytes() - reserved_bytes()
    )
    return max(0, free)


def reserve(num_bytes):
    """Atomically reserves num_bytes of disk headroom for one upload
    about to start. Raises DiskSpaceExceeded, with nothing left reserved,
    if it doesn't fit right now. Always release(num_bytes) once the
    upload is done (success or failure) - callers do this in a finally
    block, same pattern as rate_limit.py's own callers use for their
    budgets."""
    redis = connect_to_redis()
    new_total = redis.incrby(RESERVED_KEY, num_bytes)
    margin = zenodo_config.disk_safety_margin_bytes()
    if new_total > disk_free_bytes() - margin:
        redis.decrby(RESERVED_KEY, num_bytes)
        log.warning(
            'Zenodo upload rejected for lack of disk space: %d bytes '
            'requested, reserved total would have been %d, margin %d',
            num_bytes,
            new_total,
            margin,
        )
        raise DiskSpaceExceeded(
            'Not enough disk space available on the server right now for '
            'a file this size - try again shortly or with a smaller file.'
        )
    return num_bytes


def release(num_bytes):
    """Releases a reservation previously returned by reserve(). A no-op
    for falsy num_bytes so callers can unconditionally call this in a
    finally block even on a code path that never reached reserve()."""
    if not num_bytes:
        return
    redis = connect_to_redis()
    redis.decrby(RESERVED_KEY, num_bytes)
