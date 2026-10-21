![CKAN](https://img.shields.io/badge/CKAN-2.12.0-orange)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

# ckanext-zenodo

Stores a dataset's large or non-tabular files on [Zenodo](https://zenodo.org/)
instead of in CKAN's own storage, and publishes them there - with a DOI,
in a Zenodo community - when the dataset is made public. The dataset stays
a normal, searchable CKAN dataset: each Zenodo file appears in it as a
resource linking to Zenodo, with a preview for images, audio, video and
PDF. Built for the FAIR3R catalogue, whose FDF metadata it maps into the
Zenodo record.

## How it works

- **Routing**: a file goes to Zenodo when it's larger than
  `ckan.max_resource_size`, or in a format the data viewer can't read
  (not in `ckanext.xloader.formats`), or when the user switches
  "Zenodo" on. Everything else stays in CKAN. (Without
  `ckanext.xloader.formats`, no format counts as readable, so every file
  goes to Zenodo.)
- **Upload**: the file is streamed through the CKAN server into a draft
  Zenodo deposition kept for the dataset, by one shared Zenodo account.
  Nothing is public yet.
- **Publish**: when the dataset is active and public, a background job
  writes the metadata, publishes the deposition (Zenodo mints a DOI),
  files it into the community and creates the CKAN resources.
- **Later changes**: adding, replacing or deleting a file publishes a new
  version of the same record. Resources link to the record's concept DOI,
  which always resolves to the latest version.
- **Dataset deleted**: unpublished Zenodo files are deleted with it;
  published ones stay on Zenodo (the user is told so).

The full specification is in [docs/](https://github.com/Phen-ICS/ckanext-zenodo/blob/main/docs/README.md): architecture, every
workflow step by step, metadata mapping, security, configuration and
operations, and the Zenodo API behaviour it relies on.

## Requirements

- CKAN 2.12 with its Redis and background worker (`ckan jobs worker`)
  running - publishing and deletions run as background jobs.
- A Zenodo account and a personal access token (Zenodo -> Applications ->
  Personal access tokens) allowed to create and publish depositions
  (`deposit:write`, `deposit:actions`). Use
  [sandbox.zenodo.org](https://sandbox.zenodo.org/) for testing: it's a
  separate site, with its own account, token and community.
- A Zenodo community, of which that account is owner, manager or curator
  (needed to accept each record into it).
- Recommended: **[ckanext-fair3r](https://github.com/Phen-ICS/ckanext-fair3r)**
  ([PyPI](https://pypi.org/project/ckanext-fair3r/)), for a richer upload
  form (several files at once, capacity badge), the "Publish" button,
  "Download all" and the FDF metadata in the Zenodo record. Not required:
  this plugin adds its own Zenodo switch to CKAN's "new resource" form. See
  [Relationship to ckanext-fair3r](#relationship-to-ckanext-fair3r).
- Optional: **[ckanext-doi](https://github.com/Phen-ICS/ckanext-doi)**
  (PyPI: `ckanext-fair3r-doi`): the Zenodo record links back to the
  dataset's own DataCite DOI.

## Installation

1. Install the package in CKAN's virtualenv:

   ```shell
   pip install ckanext-zenodo
   ```

2. Enable the plugin, with CKAN's view plugins used for the previews, in
   `ckan.ini`:

   ```ini
   ckan.plugins = ... zenodo image_view audio_view video_view pdf_view
   ```

3. Configure the Zenodo account (sandbox here; for zenodo.org set
   `test_mode = false` and `ckanext.zenodo.access_token` instead):

   ```ini
   ckanext.zenodo.test_mode = true
   ckanext.zenodo.sandbox_access_token = <token>
   ckanext.zenodo.community_identifier = fair3r
   ```

   Every other setting has a default - see
   [docs/operations.md](https://github.com/Phen-ICS/ckanext-zenodo/blob/main/docs/operations.md#configuration-ckanini).

4. Create the table:

   ```shell
   ckan -c /path/to/ckan.ini db upgrade -p zenodo
   ```

5. Retry failed publishes every 30 minutes, as the CKAN user (cron):

   ```cron
   */30 * * * * /path/to/venv/bin/ckan -c /path/to/ckan.ini zenodo retry-pending
   ```

6. Let large uploads through: the reverse proxy's body size limit for
   `/zenodo/upload/` must allow `ckanext.zenodo.max_file_size_bytes`
   (10 GB by default), and the server needs that much free disk on top of
   `ckanext.zenodo.disk_safety_margin_bytes` (5 GB) - each upload is
   written to a temporary file before being sent on.

7. Restart CKAN and its worker, then check the setup against Zenodo:

   ```shell
   ckan -c /path/to/ckan.ini zenodo check
   ```

   It verifies the token, its access to depositions, the community and
   the account's role in it, and exits 1 on the first problem.

## Commands

| Command | Use |
|---|---|
| `ckan zenodo check` | Verify the token and community against Zenodo |
| `ckan zenodo retry-pending [--include-failed]` | Re-queue failed publishes (cron). Gives up after `ckanext.zenodo.max_publish_attempts` (10) unless `--include-failed` |
| `ckan zenodo disk-reservations show` / `clear` | Disk space reserved by uploads in flight / reset it after a crashed worker |

## Relationship to ckanext-fair3r

Not a dependency: the plugin works on its own, but
[ckanext-fair3r](https://github.com/Phen-ICS/ckanext-fair3r) is recommended,
and is what this plugin was built for. What changes with and without it:

- **Upload form.** Without ckanext-fair3r, CKAN's "new resource" form gets
  a built-in "Store this file on Zenodo" switch and a progress bar: one
  file at a time, sent to this plugin's `/zenodo/upload/<dataset>`
  endpoint when it belongs on Zenodo (see Routing), posted with the form
  as usual otherwise. With ckanext-fair3r enabled, its own resource form
  takes over instead (the built-in one is not rendered), and adds
  several files at once, the live capacity badge and a warning when a file
  name already exists. Any other client can also POST to the endpoint.
- **Around the form.** ckanext-fair3r provides the "Publish" button and
  "Download all", which uses Zenodo's own ZIP for Zenodo files. Without
  it, a dataset is published by making it public (Manage -> Visibility).
- **Metadata.** This plugin converts the dataset's FDF metadata with
  ckanext-fair3r's DataCite converter (optional import). Without it, a
  record is still published, with minimal metadata (title, tags, licence,
  submitter).

## Tests

```shell
docker exec -u ckan -it ckan-app pytest --ckan-ini=/plugins/ckanext-zenodo/test.ini /plugins/ckanext-zenodo/ckanext/zenodo/tests
```

Always pass `--ckan-ini`: the test fixtures empty the database they run
against, and refuse to run unless its name contains `test`. The
`test.ini` at the repository root is for the DEV container;
`ckanext/zenodo/tests/test.ini`, shipped in the package, is the one the
integration server uses - keep their plugin lists in sync.

## License

Released under the GNU Affero General Public License (AGPL) v3.0. See the
file `LICENSE` for details.
