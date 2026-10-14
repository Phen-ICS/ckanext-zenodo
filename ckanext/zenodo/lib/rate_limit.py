"""Redis-backed per-user rate limiting for the upload blueprint - two
counters, request count and byte volume, both per user per rolling hour
bucket. Reuses ckan.lib.redis.connect_to_redis(), same mechanism
ckanext-fair3r's draft_guard.py already uses.

Request-count alone doesn't bound total data volume: a user could send a
handful of multi-GB files well under any request-count ceiling and still
exhaust the shared Zenodo account's storage quota. Since the account is
shared, Zenodo itself has no way to throttle it per end-user - these
counters are the only real protection, not just a nicety, so both are
checked (see the implementation plan's anti-abuse section).
"""

import time

from ckan.lib.redis import connect_to_redis

REQUEST_KEY_PREFIX = 'zenodo:uploads:'
BYTES_KEY_PREFIX = 'zenodo:bytes:'
WINDOW_SECONDS = 3600


class RateLimitExceeded(Exception):
    """Raised by the check_*/add_* functions below when a user has hit
    their cap for the current hour."""


def _hour_bucket():
    return int(time.time() // WINDOW_SECONDS)


def check_and_increment_request(user_id, max_per_hour):
    """Counts *every* attempt, not just successful uploads - a flood of
    cheap-but-failing requests should still be throttled. Raises
    RateLimitExceeded if this attempt pushes the user over the cap."""
    redis = connect_to_redis()
    key = f'{REQUEST_KEY_PREFIX}{user_id}:{_hour_bucket()}'
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, WINDOW_SECONDS)
    if count > max_per_hour:
        raise RateLimitExceeded(f'Upload rate limit exceeded ({max_per_hour}/hour)')


def check_bytes_budget(user_id, max_bytes):
    """Raises RateLimitExceeded if this user has already exhausted their
    byte budget for the current hour - checked up front (before accepting
    any bytes of this particular request) in addition to add_bytes()
    below, which is checked incrementally as data actually streams."""
    redis = connect_to_redis()
    key = f'{BYTES_KEY_PREFIX}{user_id}:{_hour_bucket()}'
    used = int(redis.get(key) or 0)
    if used >= max_bytes:
        raise RateLimitExceeded(
            f'Upload byte-volume rate limit exceeded ({max_bytes} bytes/hour)'
        )


def add_bytes(user_id, num_bytes, max_bytes):
    """Adds num_bytes to this user's hour budget. Raises
    RateLimitExceeded if that pushes the total over the cap - the bytes
    are still added (there's no "undo" once a file has actually been
    read), so callers should call this as early as the real size is
    known, not after the fact."""
    redis = connect_to_redis()
    key = f'{BYTES_KEY_PREFIX}{user_id}:{_hour_bucket()}'
    new_total = redis.incrby(key, num_bytes)
    if new_total == num_bytes:
        redis.expire(key, WINDOW_SECONDS)
    if new_total > max_bytes:
        raise RateLimitExceeded(
            f'Upload byte-volume rate limit exceeded ({max_bytes} bytes/hour)'
        )
