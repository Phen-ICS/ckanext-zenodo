# Security

Every record is deposited by **one shared Zenodo account**, and a
published record is permanent (its DOI can't be withdrawn from here).
Zenodo can't tell the site's users apart, so it can't limit them: all the
protection has to happen in CKAN, before anything reaches Zenodo.

## Who may do what

| Action | Required |
|---|---|
| Upload a file to Zenodo | Logged in, and CKAN's `resource_create` permission on the dataset - the same as adding any resource |
| Remove a pending (unpublished) file | Logged in, `package_update` permission on the dataset |
| Delete a published file (CKAN resource) | CKAN's `resource_delete` rule, except the last file of a published record (refused) |
| Publish to Zenodo | Nobody directly: it follows from the dataset becoming active and public, which requires CKAN's own permission to change its visibility |
| See upload capacity | Logged in |
| CLI commands | Server access |

The publish and delete jobs run as CKAN's site user, with authorization
checks skipped - they only act on what a permitted user already did.

## Anti-abuse limits on uploads

Checked on the server for every upload, whatever the browser decided
(order in [workflows.md](workflows.md#upload---post-zenodouploaddataset)):

1. **CKAN permission** before any byte is accepted.
2. **Per-user hourly budgets** in Redis: number of upload requests
   (`max_uploads_per_hour`, default 20 - every attempt counts, failed
   ones included) and bytes (`max_bytes_per_hour`, default 20 GB).
   Windows are fixed clock hours, not a sliding hour.
3. **Size and format re-checked**: a file small and readable enough for
   CKAN is refused unless the user explicitly chose Zenodo; no file over
   `max_file_size_bytes` (default 10 GB); not over Zenodo's per-record
   limits (`max_record_bytes` 50 GB, `max_files_per_record` 100).
4. **Server disk reservation**: the whole request size is reserved in a
   counter shared by all users and workers before the body is read, and
   refused if it would leave less than `disk_safety_margin_bytes` free.
   Several users within their own budgets can't fill the disk together.

The browser-side routing and messages are a convenience, never trusted.

## Secrets

- The Zenodo token is only in `ckan.ini`
  (`ckanext.zenodo.sandbox_access_token` / `ckanext.zenodo.access_token`),
  written at deploy time from CI variables - never in the browser, the
  database, job arguments or URLs. It's sent in the `Authorization`
  header (never as `?access_token=`, which can end up in proxy logs).
- Background jobs receive ids and filenames only; they read the token
  from the configuration when they run.

## Output handling

- Zenodo resource descriptions are rendered by the templates from parts
  (`zenodo_description_parts`), autoescaped, without `Markup`; only an
  `https://` link is turned into a link - a description edited by hand
  into anything else falls back to CKAN's sanitized Markdown.
- The Zenodo record's description escapes every value it's built from
  (display name, FDF form, tags): a user can't place links or markup in a
  public record published under the shared account.
- A publish job checks again, when it runs, that the dataset is still
  active and public: one made private or deleted after being queued is
  never published.
- Filenames are percent-encoded in Zenodo URLs, so `#`, `?` or `/` in a
  name can't change which file is written.
- Uploaded files never land in CKAN's storage; the request body is a
  temporary file deleted when the request ends.

## Known limitations

- **Uploads transit through the server's disk.** The browser can't
  upload straight to Zenodo without the shared token, so each upload is
  spooled to a temporary file first. Disk space must be sized for the
  largest accepted file plus the safety margin.
- **Published files can't be removed from Zenodo.** Deleting a file or
  the dataset only removes it from new versions; earlier versions keep
  it. Withdrawing a record needs Zenodo support.
- **The record's metadata isn't updated when only the dataset changes.**
  It's sent on the next new version (see
  [metadata-mapping.md](metadata-mapping.md#when-its-sent)).
- **A single account and token for the whole site.** Revoking it stops
  every upload and publish; `ckan zenodo check` verifies it.

## Before using production Zenodo

Review the four limits above with production values, confirm disk space
on the server, set `test_mode = false` with a zenodo.org token and
community, and run `ckan zenodo check`.
