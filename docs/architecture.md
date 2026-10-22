# Architecture

## Components

```
Browser (resource form: this plugin's zenodo-upload.js, or ckanext-fair3r's resource-upload.js)
  |  POST /zenodo/upload/<dataset>   (file, multipart, with progress)
  v
CKAN web process - blueprints/zenodo_upload.py
  |  checks: login, permission, rate limits, record limits, disk space,
  |          size/format; then streams the file
  v
Zenodo bucket API (draft deposition of the dataset)

CKAN action (package_update / package_patch ...) -> after_dataset_update hook
  |  dataset active + public + pending files?  -> enqueue job
  v
RQ worker - publish_guard._publish_deposition
  |  metadata, publish, community, CKAN resources, previews
  v
Zenodo deposit / records / communities APIs

cron (every 30 min) - ckan zenodo retry-pending -> re-enqueues failed publishes
```

Rules the design follows everywhere:

- **No Zenodo call from inside a CKAN hook.** Hooks run inside the
  caller's open database transaction; a slow HTTP call there would block
  the user's request and can't safely trigger other CKAN actions. Hooks
  only enqueue an RQ job. The upload blueprint is the one place that
  calls Zenodo synchronously - it's a plain Flask view, not a hook, and
  the user is waiting for the upload anyway.
- **The browser never talks to Zenodo with the shared token.** Uploads go
  through the CKAN server, which adds the token. Zenodo has no scoped or
  delegated token that could safely be handed to a browser.
- **Background jobs act as CKAN's site user**, so resource changes they
  make have someone to be attributed to in the activity stream.

## CKAN interfaces

Declared in `plugin.py`:

| Interface | What it's used for |
|---|---|
| `IBlueprint` | `/zenodo/upload/<dataset>` (POST), `/zenodo/upload/<dataset>/<filename>` (DELETE), `/zenodo/upload-capacity` (GET) |
| `IPackageController.after_dataset_update` | Enqueue the publish job when a dataset with pending files is active and public |
| `IResourceController.before_resource_delete` / `after_resource_delete` | Remove a published file from Zenodo once its CKAN resource is really deleted |
| `IActions` (chained) | `package_update`, `package_delete`, `dataset_purge` - what happens on Zenodo when a dataset is deleted, purged or made private |
| `IAuthFunctions` (chained) | `resource_delete` - refuse deleting the last file of a published record |
| `IXloader.can_upload` (if ckanext-xloader is installed) | Stop xloader from loading a Zenodo Link resource as tabular data |
| `ITemplateHelpers` | Helpers listed below |
| `IClick` | `ckan zenodo ...` commands |
| `ITranslation` | English source strings, French catalogue in `i18n/` |
| `IConfigurer` | Template directory |

Template helpers: `zenodo_pending_deposition_for_package`,
`zenodo_notice_for_package`, `zenodo_delete_impact`,
`zenodo_description_parts`.

Templates overridden:

| Template | Change |
|---|---|
| `package/snippets/resources_list.html` | One-time notice after a publish; list of pending files with the reason they aren't published yet (and a delete button for editors) |
| `package/snippets/resource_item.html`, `package/resource_read.html` | Zenodo resources show "Zenodo · DOI <link>" with a clickable link opening in a new tab |
| `package/confirm_delete.html` | Warns what deleting the dataset does to its Zenodo files |

## Modules

| Module | Responsibility |
|---|---|
| `blueprints/zenodo_upload.py` | Upload, pending-file delete and capacity endpoints; all upload checks |
| `lib/zenodo_client.py` | Thin Zenodo REST client (no CKAN import): retries with exponential backoff on 429/5xx/network errors, bearer token in the `Authorization` header |
| `lib/zenodo_config.py` | Reads every `ckanext.zenodo.*` setting with its default |
| `lib/publish_guard.py` | Publish trigger and job, metadata assembly, CKAN resource creation, community acceptance, Zenodo URLs |
| `lib/metadata_mapper.py` | DataCite-shaped metadata -> Zenodo metadata (pure, no CKAN import) |
| `lib/delete_zenodo_file.py` | Delete a pending file; delete a published file through a new version |
| `lib/lifecycle_guard.py` | Chained `package_update` / `package_delete` / `dataset_purge` |
| `lib/media_preview.py` | Image/audio/video previews of Zenodo files through CKAN's core view plugins; removes the unusable PDF view |
| `lib/xloader_guard.py` | `IXloader.can_upload` |
| `lib/auth.py` | Chained `resource_delete` auth |
| `lib/rate_limit.py` | Per-user hourly request and byte budgets (Redis) |
| `lib/disk_guard.py` | Server-wide disk-space reservation for uploads in flight (Redis) |
| `lib/record_limits.py` | Zenodo's per-record total size and file count (pure) |
| `lib/config_check.py` | `ckan zenodo check` |
| `lib/helpers.py` | Template helpers |
| `model/` | `fair_zenodo_deposition` table and its queries |
| `cli.py` | `ckan zenodo` commands |
| `migration/zenodo/` | Alembic migrations (`ckan db upgrade -p zenodo`) |

## Data model

One table, `fair_zenodo_deposition`, one row per dataset that has at
least one file on Zenodo. Deleted with the dataset (`ON DELETE CASCADE`
on `package_id`).

| Column | Meaning |
|---|---|
| `package_id` (PK, FK `package.id`) | The dataset |
| `zenodo_deposition_id` | The deposition currently being worked on: the original draft, the published deposition, or a new-version draft |
| `bucket_url` | Its file bucket |
| `zenodo_record_id` | Record id of the latest published version (null until first publish) |
| `concept_record_id` | Concept record id, shared by every version (null until first publish); what CKAN resources link to |
| `zenodo_record_doi` | DOI of the last publish |
| `is_sandbox` | Whether this deposition lives on the sandbox (fixed at creation) |
| `published` | False while files are waiting to be published (first publish or a pending new version) |
| `file_count`, `filenames` | Files recorded for the deposition (JSON list); a re-upload under the same name isn't counted twice |
| `last_metadata_hash` | Left in the database, not used (removed from the model) |
| `pending_notice` | One-time message for the dataset page set by a background job (`published`, `new_version_published`), cleared when shown |
| `publish_attempts` | Publish jobs started since the last successful publish |
| `created`, `updated` | Timestamps |

A row with `concept_record_id` set has been published at least once:
its files can no longer be deleted from Zenodo, only superseded by a new
version.

## Dependencies on other extensions

All soft: the plugin loads and works without them, with less.

| Extension | Used for | Without it |
|---|---|---|
| ckanext-fair3r | FDF metadata (`DataCiteConverter.fdf_to_datacite`); a richer upload form (several files, capacity badge, duplicate-name warning); "Publish" button and "Download all" | Zenodo record gets minimal metadata (title, submitter); the built-in upload form (one file at a time, `zenodo-upload.js`) is used instead; no "Publish" button or "Download all" |
| ckanext-doi | Link from the Zenodo record back to the dataset's DataCite DOI, through its test or production resolver | No back-link |
| ckanext-xloader | - (the plugin only opts its Link resources out of it) | Nothing to opt out of |
| CKAN core `image_view`, `audio_view`, `video_view` | File previews on the resource page | No preview for that kind of file |

Redis (CKAN's own) holds the rate-limit and disk-reservation counters;
the RQ worker (CKAN's own) runs the background jobs.
