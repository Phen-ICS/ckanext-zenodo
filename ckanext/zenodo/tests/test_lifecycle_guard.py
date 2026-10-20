"""DB-backed tests for purge / make-private handling and the
retry-pending command - --ckan-ini=/plugins/ckanext-zenodo/test.ini
required (conftest.py guard). Zenodo and RQ are monkeypatched."""

from datetime import datetime, timedelta, timezone

import flask
import pytest
from ckan import model
from ckan.plugins import toolkit
from ckan.tests import factories
from ckan.tests.helpers import call_action
from click.testing import CliRunner

from ckanext.zenodo import cli
from ckanext.zenodo.lib import lifecycle_guard, zenodo_client
from ckanext.zenodo.model import ZenodoDepositionQuery
from ckanext.zenodo.model.zenodo_deposition import ZenodoDeposition

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(
        toolkit, 'enqueue_job', lambda fn, kwargs=None, **kw: calls.append((fn, kwargs))
    )
    return calls


def _purge(dataset):
    call_action('package_delete', {'ignore_auth': True}, id=dataset['id'])
    call_action('dataset_purge', {'ignore_auth': True}, id=dataset['id'])


def test_purge_of_a_never_published_dataset_deletes_its_zenodo_draft(enqueued):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'a.zip')

    _purge(dataset)

    assert enqueued == [
        (
            lifecycle_guard._remove_unpublished_draft,
            {'deposition_id': 'dep-1', 'sandbox': True, 'ever_published': False},
        )
    ]


def test_purge_of_a_published_dataset_leaves_zenodo_alone(enqueued):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    _purge(dataset)

    assert enqueued == []


def test_purge_discards_a_pending_new_version_draft(enqueued):
    # Published once, then a file was added: a new-version draft is
    # pending on top of the published record.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-2', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    ZenodoDepositionQuery.update_package(dataset['id'], published=False)

    _purge(dataset)

    assert enqueued[0][1] == {
        'deposition_id': 'dep-2',
        'sandbox': True,
        'ever_published': True,
    }


def test_purge_without_any_deposition_does_nothing(enqueued):
    _purge(factories.Dataset())

    assert enqueued == []


@pytest.mark.parametrize(
    ('ever_published', 'expected'),
    [(False, 'delete_deposition'), (True, 'discard_deposition')],
)
def test_remove_unpublished_draft_picks_the_right_call(
    monkeypatch, ever_published, expected
):
    calls = []
    for name in ('delete_deposition', 'discard_deposition'):
        monkeypatch.setattr(
            zenodo_client, name, lambda *a, _n=name, **k: calls.append(_n)
        )

    lifecycle_guard._remove_unpublished_draft('dep-1', True, ever_published)

    assert calls == [expected]


def test_making_private_a_dataset_already_on_zenodo_warns(app):
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'], private=False)
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )
    model.repo.commit()

    with app.flask_app.test_request_context():
        call_action(
            'package_patch', {'ignore_auth': True}, id=dataset['id'], private=True
        )
        messages = flask.get_flashed_messages()

    assert len(messages) == 1
    assert 'stay public' in messages[0]
    assert '10.5281/zenodo.1' in messages[0]


def test_making_private_a_dataset_not_on_zenodo_does_not_warn(app):
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'], private=False)

    with app.flask_app.test_request_context():
        call_action(
            'package_patch', {'ignore_auth': True}, id=dataset['id'], private=True
        )
        messages = flask.get_flashed_messages()

    assert messages == []


def _pending(private=False, minutes_old=60, attempts=0):
    org = factories.Organization()
    dataset = factories.Dataset(owner_org=org['id'], private=private)
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'a.zip')
    model.Session.query(ZenodoDeposition).filter(
        ZenodoDeposition.package_id == dataset['id']
    ).update(
        {
            'updated': datetime.now(timezone.utc) - timedelta(minutes=minutes_old),
            'publish_attempts': attempts,
        }
    )
    model.repo.commit()
    return dataset


def test_retry_pending_requeues_only_stuck_public_datasets(enqueued):
    stuck = _pending()
    _pending(private=True)  # not published on purpose - must wait
    _pending(minutes_old=1)  # probably still queued

    result = CliRunner().invoke(cli.retry_pending, [])

    assert result.exit_code == 0, result.output
    assert [kwargs for _, kwargs in enqueued] == [{'package_id': stuck['id']}]


@pytest.mark.ckan_config('ckanext.zenodo.max_publish_attempts', '3')
def test_retry_pending_gives_up_after_max_publish_attempts(enqueued):
    retried = _pending(attempts=2)
    given_up = _pending(attempts=3)

    result = CliRunner().invoke(cli.retry_pending, [])

    assert result.exit_code == 0, result.output
    assert [kwargs for _, kwargs in enqueued] == [{'package_id': retried['id']}]
    assert f'Not retried: {given_up["name"]}' in result.output


@pytest.mark.ckan_config('ckanext.zenodo.max_publish_attempts', '3')
def test_retry_pending_include_failed_starts_the_count_again(enqueued):
    given_up = _pending(attempts=5)

    result = CliRunner().invoke(cli.retry_pending, ['--include-failed'])

    assert result.exit_code == 0, result.output
    assert [kwargs for _, kwargs in enqueued] == [{'package_id': given_up['id']}]
    record = ZenodoDepositionQuery.read_package(given_up['id'])
    assert record.publish_attempts == 0


def test_retry_pending_respects_the_limit(enqueued):
    for _ in range(3):
        _pending()

    CliRunner().invoke(cli.retry_pending, ['--limit', '2'])

    assert len(enqueued) == 2


def test_delete_by_the_user_removes_the_unpublished_draft_right_away(enqueued):
    # Not waiting for a sysadmin to purge the trash.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'a.zip')

    call_action('package_delete', {'ignore_auth': True}, id=dataset['id'])

    assert enqueued == [
        (
            lifecycle_guard._remove_unpublished_draft,
            {'deposition_id': 'dep-1', 'sandbox': True, 'ever_published': False},
        )
    ]
    # Gone locally too: a dataset restored from the trash mustn't think
    # it still has those files.
    assert ZenodoDepositionQuery.read_package(dataset['id']) is None


def test_bulk_delete_also_removes_the_unpublished_draft(enqueued):
    # The organization's bulk Delete is package_patch(state=deleted),
    # never package_delete.
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.add_file(dataset['id'], 'a.zip')

    call_action(
        'package_patch', {'ignore_auth': True}, id=dataset['id'], state='deleted'
    )

    assert [kwargs['deposition_id'] for _, kwargs in enqueued] == ['dep-1']
    assert ZenodoDepositionQuery.read_package(dataset['id']) is None


def test_delete_of_a_published_dataset_keeps_its_zenodo_record(enqueued):
    dataset = factories.Dataset()
    ZenodoDepositionQuery.create(dataset['id'], 'dep-1', 'https://example.test/bucket')
    ZenodoDepositionQuery.mark_published(
        dataset['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='concept-1'
    )

    call_action('package_delete', {'ignore_auth': True}, id=dataset['id'])

    assert enqueued == []
    assert ZenodoDepositionQuery.read_package(dataset['id']) is not None


def test_delete_impact_for_the_confirmation_page():
    from ckanext.zenodo.lib.helpers import zenodo_delete_impact

    nothing, draft, published = (factories.Dataset() for _ in range(3))
    ZenodoDepositionQuery.create(draft['id'], 'dep-1', 'https://example.test/b')
    ZenodoDepositionQuery.add_file(draft['id'], 'a.zip')
    ZenodoDepositionQuery.create(published['id'], 'dep-2', 'https://example.test/b')
    ZenodoDepositionQuery.mark_published(
        published['id'], 'rec-1', '10.5281/zenodo.1', concept_record_id='c-1'
    )

    assert zenodo_delete_impact(nothing) is None
    assert zenodo_delete_impact(draft) == 'draft'
    assert zenodo_delete_impact(published) == 'published'
