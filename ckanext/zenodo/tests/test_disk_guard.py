"""DB-backed only because disk_safety_margin_bytes() reads ckan.config,
which needs a loaded CKAN app - same reason test_zenodo_config.py needs
it. Redis and real disk free space are both faked, so nothing here
depends on reachable infrastructure or the test host's actual disk."""

import pytest

from ckanext.zenodo.lib import disk_guard


class FakeRedis:
    """Minimal in-memory stand-in - just enough of the Redis API
    disk_guard.py actually calls (get/incrby/decrby)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        value = self.store.get(key)
        return str(value).encode() if value is not None else None

    def incrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    def decrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) - amount
        return self.store[key]


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(disk_guard, 'connect_to_redis', lambda: fake)
    return fake


@pytest.fixture
def fake_disk_free(monkeypatch):
    """Deterministic free space, independent of whatever's actually free
    on the machine running the tests. Returns a mutable holder so a test
    can change the value mid-test."""
    free = {'bytes': 20 * 1024**3}  # 20 GB
    monkeypatch.setattr(disk_guard, 'disk_free_bytes', lambda: free['bytes'])
    return free


@pytest.mark.ckan_config('ckanext.zenodo.disk_safety_margin_bytes', str(5 * 1024**3))
def test_available_bytes_subtracts_margin_and_reserved(fake_redis, fake_disk_free):
    disk_guard.reserve(3 * 1024**3)

    # 20 GB free - 5 GB margin - 3 GB already reserved = 12 GB
    assert disk_guard.available_bytes() == 12 * 1024**3


@pytest.mark.ckan_config('ckanext.zenodo.disk_safety_margin_bytes', str(5 * 1024**3))
def test_reserve_succeeds_when_it_fits(fake_redis, fake_disk_free):
    reserved = disk_guard.reserve(10 * 1024**3)

    assert reserved == 10 * 1024**3
    assert disk_guard.reserved_bytes() == 10 * 1024**3


@pytest.mark.ckan_config('ckanext.zenodo.disk_safety_margin_bytes', str(5 * 1024**3))
def test_reserve_raises_and_rolls_back_when_it_does_not_fit(fake_redis, fake_disk_free):
    # 20 GB free - 5 GB margin = 15 GB available; this asks for 16 GB.
    with pytest.raises(disk_guard.DiskSpaceExceeded):
        disk_guard.reserve(16 * 1024**3)

    # The failed attempt must not leave anything reserved behind.
    assert disk_guard.reserved_bytes() == 0


@pytest.mark.ckan_config('ckanext.zenodo.disk_safety_margin_bytes', str(5 * 1024**3))
def test_second_concurrent_reservation_is_rejected_once_the_first_fills_the_budget(
    fake_redis, fake_disk_free
):
    # 20 GB free - 5 GB margin = 15 GB available.
    disk_guard.reserve(10 * 1024**3)

    with pytest.raises(disk_guard.DiskSpaceExceeded):
        disk_guard.reserve(10 * 1024**3)

    # The first reservation (still genuinely in flight) must survive the
    # second, rejected one.
    assert disk_guard.reserved_bytes() == 10 * 1024**3


@pytest.mark.ckan_config('ckanext.zenodo.disk_safety_margin_bytes', str(5 * 1024**3))
def test_release_frees_up_room_for_a_later_reservation(fake_redis, fake_disk_free):
    first = disk_guard.reserve(10 * 1024**3)
    disk_guard.release(first)

    second = disk_guard.reserve(10 * 1024**3)

    assert second == 10 * 1024**3
    assert disk_guard.reserved_bytes() == 10 * 1024**3


def test_release_of_falsy_amount_is_a_no_op(fake_redis):
    disk_guard.release(0)
    disk_guard.release(None)

    assert disk_guard.reserved_bytes() == 0
