"""`ckan zenodo check` - Zenodo and the config are monkeypatched, no
network, no DB."""

import pytest

from ckanext.zenodo.lib import config_check, zenodo_client, zenodo_config

MEMBERS_OWNER = [{'member': {'type': 'user', 'id': '42'}, 'role': 'owner'}]


@pytest.fixture
def zenodo(monkeypatch):
    """A working setup; each test breaks one step."""
    state = {
        'token': 'tok',
        'me': {'id': 42},
        'community': {'id': 'c-1'},
        'members': MEMBERS_OWNER,
        'fail': None,
    }

    def answer(name, value):
        def fn(*a, **k):
            if state['fail'] == name:
                raise zenodo_client.ZenodoError('Zenodo returned 401: nope')
            return state[value] if value else {}

        return fn

    def access_token(sandbox):
        if not state['token']:
            raise RuntimeError('not configured')  # what the real one does
        return state['token']

    monkeypatch.setattr(zenodo_config, 'sandbox_mode', lambda: True)
    monkeypatch.setattr(
        zenodo_config, 'is_configured', lambda sandbox=None: bool(state['token'])
    )
    monkeypatch.setattr(zenodo_config, 'access_token', access_token)
    monkeypatch.setattr(zenodo_config, 'community_identifier', lambda: 'fair3r')
    monkeypatch.setattr(zenodo_client, 'get_me', answer('get_me', 'me'))
    monkeypatch.setattr(
        zenodo_client, 'list_depositions', answer('list_depositions', None)
    )
    monkeypatch.setattr(
        zenodo_client, 'get_community', answer('get_community', 'community')
    )
    monkeypatch.setattr(
        zenodo_client, 'list_community_members', answer('members', 'members')
    )
    return state


def test_a_working_setup_passes_every_check(zenodo):
    results = config_check.run_checks()

    assert all(ok for ok, _ in results)
    assert len(results) == 5
    assert results[-1][1] == "Account 42 is owner of 'fair3r'"


def test_missing_token_stops_at_the_first_check(zenodo):
    zenodo['token'] = None

    results = config_check.run_checks()

    assert results == [
        (
            False,
            'ckanext.zenodo.sandbox_access_token is not set (sandbox.zenodo.org mode)',
        )
    ]


def test_rejected_token(zenodo):
    zenodo['fail'] = 'get_me'

    ok, message = config_check.run_checks()[-1]

    assert not ok
    assert 'rejects the token' in message


def test_unknown_community(zenodo):
    zenodo['community'] = None

    ok, message = config_check.run_checks()[-1]

    assert not ok
    assert message == "Community 'fair3r' does not exist on sandbox.zenodo.org"


@pytest.mark.parametrize(
    'members, shown',
    [
        ([{'member': {'type': 'user', 'id': '42'}, 'role': 'reader'}], 'is reader'),
        ([{'member': {'type': 'user', 'id': '7'}, 'role': 'owner'}], 'is not a member'),
    ],
)
def test_account_that_cannot_accept_inclusion_requests(zenodo, members, shown):
    # The silent case: publishing works, accepting into the community 403s.
    zenodo['members'] = members

    ok, message = config_check.run_checks()[-1]

    assert not ok
    assert f'Account 42 {shown}' in message


@pytest.mark.parametrize('role', config_check.CURATING_ROLES)
def test_every_curating_role_is_enough(zenodo, role):
    zenodo['members'] = [{'member': {'type': 'user', 'id': '42'}, 'role': role}]

    assert config_check.run_checks()[-1][0] is True
