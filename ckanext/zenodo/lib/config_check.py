"""`ckan zenodo check` - is this site's Zenodo setup actually usable?

Each step needs the previous one, so the first failure stops the run:
a token is configured for the current mode, Zenodo accepts it, it can
use the deposit API, the community exists, and the token's account may
accept inclusion requests into it (owner, manager or curator).

The last one is the setup mistake that's otherwise silent: with a token
from the wrong account, publishing still works and only the automatic
community acceptance fails, with a 403 in the worker log.
"""

from ckanext.zenodo.lib import zenodo_client, zenodo_config

# Community roles that can accept an inclusion request.
CURATING_ROLES = ('owner', 'manager', 'curator')


def run_checks():
    """Returns [(ok, message), ...], stopping at the first failure."""
    results = []

    def done(ok, message):
        results.append((ok, message))
        return results

    sandbox = zenodo_config.sandbox_mode()
    site = 'sandbox.zenodo.org' if sandbox else 'zenodo.org'
    key = 'sandbox_access_token' if sandbox else 'access_token'

    if not zenodo_config.is_configured(sandbox):
        return done(False, f'ckanext.zenodo.{key} is not set ({site} mode)')
    token = zenodo_config.access_token(sandbox)
    done(True, f'Token set for {site} (ckanext.zenodo.{key})')

    try:
        me = zenodo_client.get_me(token, sandbox=sandbox) or {}
    except zenodo_client.ZenodoError as e:
        return done(False, f'{site} rejects the token: {e}')
    account_id = str(me.get('id', ''))
    done(True, f'Token accepted - account {account_id}')

    try:
        zenodo_client.list_depositions(token, sandbox=sandbox)
    except zenodo_client.ZenodoError as e:
        return done(False, f'The token cannot use the deposit API: {e}')
    done(True, 'Token can use the deposit API')

    identifier = zenodo_config.community_identifier()
    try:
        community = zenodo_client.get_community(identifier, token, sandbox=sandbox)
    except zenodo_client.ZenodoError as e:
        return done(False, f'Could not read community {identifier!r}: {e}')
    if community is None:
        return done(False, f'Community {identifier!r} does not exist on {site}')
    done(True, f'Community {identifier!r} exists')

    try:
        members = zenodo_client.list_community_members(
            community['id'], token, sandbox=sandbox
        )
    except zenodo_client.ZenodoError as e:
        return done(False, f'Could not list the members of {identifier!r}: {e}')
    role = next(
        (
            m.get('role')
            for m in members
            if (m.get('member') or {}).get('type') == 'user'
            and str((m.get('member') or {}).get('id')) == account_id
        ),
        None,
    )
    if role not in CURATING_ROLES:
        return done(
            False,
            f'Account {account_id} is {role or "not a member"} of {identifier!r} '
            f'- it needs to be {", ".join(CURATING_ROLES)} to accept the '
            f'datasets published into it',
        )
    return done(True, f'Account {account_id} is {role} of {identifier!r}')
