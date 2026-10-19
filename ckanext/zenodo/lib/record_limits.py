"""Zenodo's own per-record limits (total size of all files, and number
of files) - distinct from zenodo_config.max_file_size_bytes(), which only
bounds a single file. Without this, several individually-acceptable
files could still add up past what Zenodo accepts for one record, and
the failure would only surface from Zenodo itself at upload or publish
time, long after the user has waited through the transfer.

Pure functions (no CKAN/Redis/Zenodo access) so the rule itself is
trivial to test; the blueprint is what fetches the deposition's real
current files and passes them in.
"""


class RecordLimitExceeded(Exception):
    """Raised by check() when one more file wouldn't fit in the record."""


def _gb(num_bytes):
    return f'{num_bytes / 1024**3:.2f} GB'


def check(existing_files, new_file_size, max_bytes, max_files):
    """existing_files: the deposition's current files as returned by
    Zenodo (dicts with a 'filesize' key). Raises RecordLimitExceeded with
    a user-facing message if adding one file of new_file_size bytes
    would exceed max_files or max_bytes."""
    count = len(existing_files)
    if count + 1 > max_files:
        raise RecordLimitExceeded(
            f'This dataset already has {count} file(s) on Zenodo, and '
            f'Zenodo allows at most {max_files} files per record.'
        )

    used = sum(int(f.get('filesize') or 0) for f in existing_files)
    if used + new_file_size > max_bytes:
        remaining = max(0, max_bytes - used)
        raise RecordLimitExceeded(
            f'Zenodo allows at most {_gb(max_bytes)} in total per record, '
            f'all files included. This dataset already has {_gb(used)} on '
            f'Zenodo, so only {_gb(remaining)} remain - this file is '
            f'{_gb(new_file_size)}.'
        )
