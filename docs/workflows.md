# Workflows

Each section describes one action as the user sees it, then what
happens on the server and on Zenodo.

## 1. Adding a file

### Where the file goes

The resource form (ckanext-fair3r's `resource-upload.js`) decides in the
browser, for each selected file, before sending anything:

- **Zenodo** if the file is larger than `ckan.max_resource_size`, or its
  format isn't one the data viewer reads (`ckanext.xloader.formats`, by
  extension or MIME type), or the user switched **Zenodo** on;
- **CKAN storage** otherwise (normal CKAN upload, data viewer, etc.).

The form shows, before upload: why files may go to Zenodo, how many of
the selected ones will, how much can be uploaded right now
(`GET /zenodo/upload-capacity`), and a warning for any file whose name
already exists in the dataset ("adds a second copy" for CKAN, "replaces
it" for a Zenodo file sent to Zenodo again). The routing thresholds come
from a `<meta name="zenodo-routing-config">` tag (`routing_config_dict()`).

The browser decision is a convenience only: the server checks everything
again. Without a Zenodo token configured, none of this applies: the form
offers no Zenodo option and every file stays in CKAN.

### Upload - `POST /zenodo/upload/<dataset>`

Checked in this order; the first failure answers with the status shown
and nothing is sent to Zenodo:

| # | Check | Failure |
|---|---|---|
| 1 | Logged in; a Zenodo token is configured | 403 / 503 |
| 2 | Dataset exists and the user may add a resource to it (`resource_create` permission) | 404 / 403 |
| 3 | Hourly request count and byte budget of this user (`rate_limit.py`) | 429 |
| 4 | Request has a size (`Content-Length`) | 400 |
| 5 | The file would fit in the Zenodo record: file count and total size, read live from Zenodo (`record_limits.py`) | 409 (502 if Zenodo can't be reached) |
| 6 | Disk space is reserved on the server for the whole request (`disk_guard.py`) - done before the body is read, since reading it writes it to a temporary file | 507 |
| 7 | A file was sent, isn't empty, isn't over `max_file_size_bytes` | 400 |
| 8 | Unless "Zenodo" was switched on: the file really is too big for CKAN or unreadable by the data viewer (otherwise: "use the normal upload") | 400 |
| 9 | Its bytes fit in the user's hourly byte budget | 429 |

Then:

1. **Find the deposition.** The dataset's row is read; if there's none,
   a new empty deposition is created on Zenodo and recorded.
2. **Choose the bucket.** If the deposition is unpublished, its own
   bucket. If it's already published, a **new version** is created on
   Zenodo first and the file goes into the new draft's bucket - a file
   written into a published record's bucket is accepted but never
   appears on the record.
3. **Stream the file** to `<bucket>/<filename>` (filename
   percent-encoded). A file with the same name replaces the existing
   one.
4. **Record it**: for a new version, the row now points at the new
   draft and `published` goes back to false - only after the upload
   succeeded, so a failure leaves the published record untouched. The
   filename is added to the list (once).
5. If a new version was created, the publish is triggered right away
   (the dataset is already public).
6. The disk reservation is released, whatever happened.

The browser shows progress per file and the result of each.

### Before the dataset is public

Files stay in the **unpublished** deposition: no DOI, not public. The
dataset page lists them under the resources, saying they'll be archived
with a public DOI once the dataset is made public. Editors can remove
one (`DELETE /zenodo/upload/<dataset>/<filename>`, needs
`package_update` permission): it's deleted from the draft on Zenodo and
from the list.

## 2. Publishing

### Trigger

`after_dataset_update` runs on every update of every dataset. It
enqueues the publish job only if the dataset is a `dataset`, **active**,
**not private**, and has a deposition that is unpublished with at least
one file. This is the same condition ckanext-doi uses to mint the
DataCite DOI, so a dataset made public gets both. Making it public by
any means works: the "Publish" button (ckanext-fair3r), Manage ->
Visibility, the organization's bulk "Make public", the API.

Because the check runs on every update, there's no special case for a
dataset created public, made public later, or edited after a failure:
the next update triggers the publish if something is still pending.

### The publish job (`_publish_deposition`, RQ)

1. Stop if the deposition is already published or gone.
2. Count the attempt (`publish_attempts + 1`, committed immediately).
3. Build the metadata (see [metadata-mapping.md](metadata-mapping.md)):
   FDF form via ckanext-fair3r, dataset title, tags, licence, the real
   submitter's name, the community, and a link to the dataset's DataCite
   DOI if ckanext-doi minted one.
4. Write the metadata to the deposition, then **publish** it. Zenodo
   mints the version DOI (and the concept DOI on the first publish).
   On a Zenodo error: log, stop; the deposition stays unpublished and
   is retried later (section 6).
5. **Community**: submit an inclusion request to the community, then
   accept it with the same account. Done explicitly on every publish -
   Zenodo only creates the request by itself on a record's very first
   publish. Never fatal: if it fails, the record is published but not
   yet in the community.
6. Mark the row published: record id, DOI, concept record id,
   `publish_attempts` back to 0. Done **before** step 7 so that xloader,
   which is called synchronously while resources are created, already
   recognises the new resources as Zenodo ones.
7. **CKAN resources**, one per file in the published version:
   - name: the filename; format: from the extension (CKAN's own format
     table); size: from Zenodo;
   - URL: the concept record page with `?preview_file=<filename>`, so
     "Go to resource" opens that file's preview on Zenodo and stays valid
     for every future version;
   - description: `Zenodo · DOI [<concept DOI>](<link>)`, plus
     ` · sandbox` on the sandbox. The link is `https://doi.org/<DOI>` in
     production; on the sandbox, whose test DOIs don't resolve, the
     sandbox record page;
   - an existing resource for the same file is kept and only updated
     (description, size), so retries and later versions don't create
     duplicates.
8. **Previews**: for image, audio, video and PDF files, the matching
   core view (`image_view`, `audio_view`, `video_view`, `pdf_view`) is
   created or re-pointed at the file's direct content URL on Zenodo for
   this version (`/api/records/<record id>/files/<name>/content`).
9. Leave a one-time notice for the dataset page ("Archived on Zenodo
   (DOI: ...)"), then commit.

Creating resources updates the dataset, which in turn lets ckanext-doi
update the DataCite metadata (sizes and formats of the Zenodo files
included).

### What the reader sees

- each Zenodo file as a normal resource with a format icon, the DOI link
  and, where possible, a preview;
- xloader never tries to load these resources (`IXloader.can_upload`
  returns false for them), so there's no "Datastore error" badge;
- "Download all" (ckanext-fair3r): CKAN-stored files as CKAN's ZIP,
  Zenodo files as Zenodo's own ZIP of the latest version
  (`/api/records/<record id>/files-archive`), downloaded straight from
  Zenodo, never through the CKAN server; both are offered when the
  dataset has both kinds.

## 3. Adding or replacing a file after publication

Same upload as section 1. Because the record is published, the file
goes into a **new version** (copy of the latest version's files and
metadata), which is published right away by the same job. The concept
DOI and the CKAN resource URLs don't change; a new resource is created
for a new file, an existing one is updated (size) for a replaced one,
previews of every file are re-pointed to the new version. Earlier
versions stay on Zenodo, frozen, under their own DOIs.

## 4. Deleting a file

### Not yet published

From the pending-files list (section 1), or nothing to do: the file only
exists in the draft.

### Published - CKAN's own "Delete resource"

1. **Permission** (`resource_delete`, chained): CKAN's normal rule, plus
   one refusal - the **last** file of a published record can't be
   deleted, because Zenodo can't publish a version without files. The
   user is asked to add a replacement first.
2. `before_resource_delete`: if the resource is one of ours (its URL is
   the concept record's, its name is a recorded file), it's noted in the
   action's context - nothing more yet.
3. CKAN deletes the resource and commits.
4. `after_resource_delete`: for each noted resource that is really gone,
   a job is enqueued. If CKAN's deletion failed, this hook isn't reached
   and Zenodo is left alone.
5. The job checks once more that the resource is deleted, then creates a
   new version, deletes the file from it, publishes it, accepts it into
   the community, updates the row and re-points the remaining previews.
   A notice "Zenodo archive updated" is left for the dataset page. The
   file remains in earlier versions on Zenodo.

## 5. Deleting, purging or making a dataset private

Handled by chained CKAN actions, which also cover the organization's
bulk actions (they go through `package_patch`):

| Action | Never published | Published |
|---|---|---|
| Delete (user, or bulk Delete) | The Zenodo draft and its files are deleted right away, with the row - nothing public is lost, and the shared account's quota isn't kept busy waiting for the trash to be emptied. Restoring the dataset brings it back without those files. | Nothing on Zenodo; message: the files stay publicly archived on Zenodo (with link). |
| Purge (sysadmin) | Same cleanup, for anything a delete didn't already remove. | Nothing deleted (a published record can't be); a pending new-version draft is discarded; message: the files stay public on Zenodo. |
| Public -> private | - | Nothing on Zenodo; message: the archived files stay public. |

The delete confirmation page says beforehand which case applies. Messages
are only shown in the web interface, not for API or CLI calls.

## 6. When a publish fails

Causes: Zenodo unavailable, rate limiting after a bulk "Make public",
invalid metadata, a configuration problem. The job logs the error and
leaves the deposition unpublished; the dataset page keeps saying the
files are "being archived".

- `ckan zenodo retry-pending` (cron, every 30 min) re-enqueues
  unpublished depositions of active public datasets untouched for 30
  minutes, at most 10 per run.
- Each attempt is counted. After `ckanext.zenodo.max_publish_attempts`
  (default 10, about 5 hours) the command stops re-enqueueing that
  dataset and lists it on each run; the dataset page says archiving
  keeps failing and to contact the administrators.
- Editing the dataset tries again (section 2). `ckan zenodo
  retry-pending --include-failed` re-enqueues all of them with a fresh
  count.
- `ckan zenodo check` tells whether the token and community are usable
  (see [operations.md](operations.md)).

A failed **upload** isn't retried by the server: the user sees the error
in the upload dialog and can upload again.
