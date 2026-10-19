"""Pure rule tests - no CKAN app, DB or network needed."""

import pytest

from ckanext.zenodo.lib import record_limits

GB = 1024**3


def test_fits_when_well_under_both_limits():
    record_limits.check([{'filesize': GB}], GB, 50 * GB, 100)


def test_exactly_at_the_size_limit_is_accepted():
    record_limits.check([{'filesize': 49 * GB}], GB, 50 * GB, 100)


def test_rejected_when_total_size_would_exceed_limit():
    with pytest.raises(record_limits.RecordLimitExceeded) as exc:
        record_limits.check([{'filesize': 45 * GB}], 10 * GB, 50 * GB, 100)

    message = str(exc.value)
    assert '50.00 GB' in message
    assert '45.00 GB' in message
    assert '5.00 GB remain' in message


def test_rejected_when_file_count_would_exceed_limit():
    files = [{'filesize': 1}] * 100

    with pytest.raises(record_limits.RecordLimitExceeded) as exc:
        record_limits.check(files, 1, 50 * GB, 100)

    assert 'at most 100 files' in str(exc.value)


def test_missing_filesize_counts_as_zero():
    record_limits.check([{'filename': 'a'}], GB, 50 * GB, 100)
