import click

from ckanext.zenodo.lib import disk_guard


def get_commands():
    return [zenodo]


@click.group()
def zenodo():
    """ckanext-zenodo maintenance commands."""


@zenodo.group(name='disk-reservations')
def disk_reservations():
    """Inspect/reset the shared disk-space reservation counter
    (disk_guard.py) that bounds how many concurrent uploads the /zenodo/
    upload/ blueprint will accept. Every normal upload releases its own
    reservation when it finishes (success or failure); this is only an
    escape hatch for the rare case a worker was killed before that could
    run, leaving a reservation stuck forever."""


@disk_reservations.command(name='show')
def show():
    """Prints the currently reserved bytes and the resulting available
    bytes, without changing anything."""
    reserved = disk_guard.reserved_bytes()
    available = disk_guard.available_bytes()
    click.echo(f'Reserved: {reserved} bytes ({reserved / 1024**3:.2f} GB)')
    click.echo(f'Available: {available} bytes ({available / 1024**3:.2f} GB)')


@disk_reservations.command(name='clear')
@click.option(
    '--yes',
    is_flag=True,
    help='Skip the confirmation prompt.',
)
def clear(yes):
    """Resets the reservation counter to 0. Only do this once you've
    confirmed (e.g. with `show`, and by checking no upload is genuinely
    in progress) that the current reserved total is stuck rather than
    real - clearing it while a real upload is still running would let a
    concurrent one overshoot the disk's actual free space."""
    reserved = disk_guard.reserved_bytes()
    if reserved == 0:
        click.echo('Nothing reserved - nothing to clear.')
        return
    click.echo(
        f'About to clear a reservation of {reserved} bytes '
        f'({reserved / 1024**3:.2f} GB).'
    )
    if not yes and not click.confirm(
        'Are you sure no upload is genuinely in progress?'
    ):
        click.echo('Aborted.')
        return
    disk_guard.release(reserved)
    click.echo('Cleared.')


@zenodo.command(name='retry-pending')
@click.option('--limit', default=10, show_default=True, help='Max datasets per run.')
@click.option(
    '--min-age',
    default=30,
    show_default=True,
    help='Only retry depositions untouched for at least this many minutes.',
)
@click.option(
    '--include-failed',
    is_flag=True,
    help='Also retry depositions past ckanext.zenodo.max_publish_attempts '
    '(their count starts again from zero).',
)
def retry_pending(limit, min_age, include_failed):
    """Re-queues the Zenodo publish of datasets that are public with files
    still waiting - i.e. a previous publish job failed (Zenodo down, rate
    limit after a big bulk "Make public"...). Meant for cron: one DB
    query, only enqueues (the RQ worker does the work, one at a time),
    capped by --limit so a run never hammers Zenodo. --min-age skips
    publishes that are probably just still queued; a job that fails
    again simply gets picked up by the next run - up to
    ckanext.zenodo.max_publish_attempts, then it's left alone (and
    listed here) until --include-failed or an edit of the dataset."""
    from datetime import datetime, timedelta, timezone

    from ckan import model
    from ckan.plugins import toolkit

    from ckanext.zenodo.lib import zenodo_config
    from ckanext.zenodo.lib.publish_guard import _publish_deposition
    from ckanext.zenodo.model.zenodo_deposition import ZenodoDeposition

    max_attempts = zenodo_config.max_publish_attempts()
    pending = (
        model.Session.query(ZenodoDeposition, model.Package.name)
        .join(model.Package, model.Package.id == ZenodoDeposition.package_id)
        .filter(
            ZenodoDeposition.published.is_(False),
            ZenodoDeposition.file_count > 0,
            model.Package.state == 'active',
            model.Package.private.is_(False),
        )
    )
    given_up = pending.filter(ZenodoDeposition.publish_attempts >= max_attempts)

    # Retried right away: resetting the count also bumps `updated`, which
    # --min-age would then skip.
    package_ids = []
    if include_failed:
        for deposition, _name in given_up:
            deposition.publish_attempts = 0
            package_ids.append(deposition.package_id)
        model.repo.commit()
    else:
        for deposition, name in given_up:
            click.secho(
                f'Not retried: {name} - {deposition.publish_attempts} failed '
                f'publish attempts (use --include-failed to retry anyway)',
                fg='yellow',
                err=True,
            )

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age)
    package_ids += [
        deposition.package_id
        for deposition, _name in pending.filter(
            ZenodoDeposition.publish_attempts < max_attempts,
            ZenodoDeposition.updated < cutoff,
        )
        .order_by(ZenodoDeposition.updated)
        .limit(limit)
        if deposition.package_id not in package_ids
    ]
    for package_id in package_ids:
        toolkit.enqueue_job(
            _publish_deposition,
            kwargs={'package_id': package_id},
            title=f'ckanext-zenodo retry-pending: publish {package_id}',
        )
    click.echo(f'Re-queued {len(package_ids)} pending Zenodo publish(es).')
