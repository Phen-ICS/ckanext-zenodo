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
