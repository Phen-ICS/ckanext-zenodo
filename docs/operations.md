# Operations

## Configuration (`ckan.ini`)

| Setting | Default | Meaning |
|---|---|---|
| `ckanext.zenodo.test_mode` | `true` | `true`: sandbox.zenodo.org; `false`: zenodo.org. Each deposition remembers where it was created. |
| `ckanext.zenodo.sandbox_access_token` | - | Token of the shared account on the sandbox (used in test mode) |
| `ckanext.zenodo.access_token` | - | Token of the shared account on zenodo.org (used when `test_mode` is false) |
| `ckanext.zenodo.community_identifier` | `fair3r` | Community every record is filed into |
| `ckanext.zenodo.max_uploads_per_hour` | `20` | Upload requests per user per clock hour |
| `ckanext.zenodo.max_bytes_per_hour` | `21474836480` (20 GB) | Bytes per user per clock hour |
| `ckanext.zenodo.max_file_size_bytes` | `10737418240` (10 GB) | Largest single file. Keep nginx's body size limit for `/zenodo/upload/` and the server's free disk in line with it. |
| `ckanext.zenodo.max_record_bytes` | `53687091200` (50 GB) | Zenodo's total size per record |
| `ckanext.zenodo.max_files_per_record` | `100` | Zenodo's file count per record |
| `ckanext.zenodo.disk_safety_margin_bytes` | `5368709120` (5 GB) | Disk space always left free by uploads |
| `ckanext.zenodo.max_publish_attempts` | `10` | Publish attempts before `retry-pending` gives up |

Also read: `ckan.max_resource_size` (MB - the routing threshold: larger
files go to Zenodo), `ckanext.xloader.formats` (formats that stay in
CKAN), `ckan.preview.image_formats` / `audio_formats` / `video_formats`
(which files get a preview).

**Without a token** for the current mode, the plugin stays out of the
way: nothing is routed to Zenodo (every file stays in CKAN, as if the
plugin weren't there), uploads to Zenodo are refused with "Zenodo isn't
set up on this site", a warning is logged at startup, and
`ckan zenodo check` reports the missing setting. So it can be enabled
before its account exists.

The token, the community and `test_mode` must belong to the same site:
a token only works on the site that issued it, and the community must
exist there.

Plugins to enable: `zenodo`, plus `image_view`, `audio_view`,
`video_view`, `pdf_view` for previews.

Database: `ckan db upgrade -p zenodo` after installing or upgrading.

## Commands

| Command | Use |
|---|---|
| `ckan zenodo check` | Verifies, against Zenodo, that a token is set for the current mode, Zenodo accepts it, it can use the deposit API, the community exists and the token's account is owner, manager or curator of it. Exits 1 on the first problem - suitable at the end of a deploy. |
| `ckan zenodo retry-pending [--limit 10] [--min-age 30] [--include-failed]` | Re-enqueues unpublished depositions of active public datasets (cron, every 30 min). Lists those past `max_publish_attempts`; `--include-failed` retries them with a fresh count. |
| `ckan zenodo disk-reservations show` | Disk space reserved by uploads in flight, and what's available |
| `ckan zenodo disk-reservations clear` | Resets the reservation counter - only if a worker died during an upload and left a reservation behind |

## Deployment

**DEV (Docker Compose)**: `ckan/scripts/entrypoint.sh` writes the
settings from `.env` (`ZENODO_SANDBOX_ACCESS_TOKEN`, `ZENODO_ACCESS_TOKEN`,
`ZENODO_COMMUNITY_IDENTIFIER`, `ZENODO_TEST_MODE`,
`ZENODO_DISK_SAFETY_MARGIN_BYTES`) and runs the migration; supervisord
runs `retry-pending` every 30 minutes.

**integration / validation / production (Ansible, fair3r-docker-alt)**:
switched on per environment with `zenodo_enabled` in
`deploy/ansible/group_vars/<env>.yml` (currently integration only);
`zenodo_test_mode` per environment. When on, the deploy installs the
package from PyPI, enables the plugins, writes the settings, runs the
migration and installs the `retry-pending` cron. Token from the CI
variable `SANDBOX_ZENODO_ACCESS_TOKEN` or `PRODUCTION_ZENODO_ACCESS_TOKEN`,
community from `ZENODO_COMMUNITY_IDENTIFIER`.

**CI**: an MR of this extension triggers the integration deploy and the
`test:integration:zenodo` job in fair3r-docker-alt with that MR's own
build. Tests on the VM use the `test.ini` shipped in the package; in DEV,
the repository root `test.ini` - keep their plugin lists in sync.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Upload fails with "Could not reach Zenodo" | Zenodo down or token rejected | `ckan zenodo check`; retry later |
| 429 on upload | User's hourly request or byte budget used up | Wait for the next hour, or raise the limits |
| 507 on upload | Not enough free disk for the upload plus the margin | Free disk space; if nothing is uploading, check `disk-reservations show` |
| 409 on upload | Record would exceed Zenodo's file count or total size | Split the files over datasets, or ask Zenodo to raise the limits for the account |
| 413 from nginx | File bigger than nginx's body size limit | Align nginx's limit for `/zenodo/upload/` with `max_file_size_bytes` |
| Files stay "being archived" | Publish job failing, or the worker not running | Worker logs; `ckan zenodo check`; `ckan zenodo retry-pending` |
| "Archiving keeps failing" on the dataset page | `max_publish_attempts` reached | Fix the cause, then edit the dataset or run `retry-pending --include-failed` |
| Record published but not in the community | Token's account can't accept inclusion requests | `ckan zenodo check`; make the account owner/manager/curator; accept the request on Zenodo |
| "Datastore error" on a Zenodo resource | xloader tried to load the link (plugin not loaded, or resource not recognised as ours) | Check the plugin is enabled; the URL must be the concept record page |
| A Zenodo resource has no preview | View plugin not enabled, or format not in `ckan.preview.*_formats` | Enable the plugin / add the format |
| Can't delete a resource: "last file of a published Zenodo record" | Zenodo can't publish a version without files | Add a replacement file first |
