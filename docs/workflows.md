# Workflows

Each section describes one action as the user sees it, then what
happens on the server and on Zenodo.

## 1. Adding a file

### Where the file goes

The resource form decides in the browser, for each selected file, before
sending anything. It is this plugin's own `zenodo-upload.js` (one file at
a time, a "Store this file on Zenodo" switch, a progress bar) or, when
ckanext-fair3r is enabled, that plugin's `resource-upload.js` (which takes
over; everything below that mentions several files, the capacity badge or
the duplicate-name warning is the latter):

- **Zenodo** if the file is larger than `ckan.max_resource_size`, or its
  format isn't one the data viewer reads (`ckanext.xloader.formats`, by
  extension or MIME type), or the user switched **Zenodo** on;
- **CKAN storage** otherwise (normal CKAN upload, data viewer, etc.).

The form shows, before upload: why files may go to Zenodo, how many of
the selected ones will, how much can be uploaded right now
(`GET /zenodo/upload-capacity`), and a warning for any file whose name
already exists in the dataset ("adds a second copy" for CKAN, "replaces
it" for a Zenodo file sent to Zenodo again). The routing thresholds come
from `routing_config_dict()`: a `<meta name="zenodo-routing-config">` tag
for ckanext-fair3r, a `data-config` attribute on the built-in form's
option block (`h.zenodo_standalone_upload_config()`).

The browser decision is a convenience only: the server checks everything
again. Without a Zenodo token configured, none of this applies: the form
offers no Zenodo option and every file stays in CKAN.

### Upload - in chunks, then sent by a background job

The web server runs one uWSGI process (1-2 GB VMs) and kills any request
over 50 s, so a large file can't go through one request. The browser
(`assets/js/zenodo-chunked-upload.js`, used by this plugin's form and by
ckanext-fair3r's) sends it in chunks instead:

1. **Start** - `POST /zenodo/uploads/<dataset>`, JSON `{filename, size,
   mimetype, force_zenodo}`. Checked in this order; the first failure
   answers with the status shown, nothing is stored:

   | # | Check | Failure |
   |---|---|---|
   | 1 | A Zenodo token is configured; logged in; the dataset exists and the user may add a resource to it (`resource_create`) | 503 / 403 / 404 |
   | 2 | Hourly request count and byte budget of this user (`rate_limit.py`) | 429 |
   | 3 | The file isn't empty, isn't over `max_file_size_bytes`, and - unless "Zenodo" was switched on - really is too big for CKAN or unreadable by the data viewer | 400 |
   | 4 | It fits in the Zenodo record: file count and total size, read live from Zenodo (`record_limits.py`) | 409 (502 if Zenodo can't be reached) |
   | 5 | Its size fits in the user's hourly byte budget | 429 |
   | 6 | Disk space is reserved for the whole file (`disk_guard.py`), until it's on Zenodo or the upload is dropped | 507 |

   A row is created in `fair_zenodo_upload` (status `receiving`).
2. **Chunks** - `PUT /zenodo/uploads/<dataset>/<upload>/chunk?offset=N`,
   16 MB each, raw body, written to `<ckan.storage_path>/zenodo_uploads/`.
   Only the user who started it; a chunk must start at or before what
   was received (416 otherwise). A chunk whose request fails (network,
   server busy: no answer, 429, 5xx) is sent again by the browser after
   2, 5, 10, 20 then 30 s; sending one again replaces it. Any other
   refusal stops the upload, and the browser drops it (`DELETE`) to free
   the disk space at once.
3. **Finish** - `POST .../finish`: all bytes in (409 otherwise), the row
   goes `queued` and a background job takes over. The browser is done:
   the file is "received".
4. **Send** (`chunked_upload.send_to_zenodo`, RQ, 6 h timeout):
   - find or create the dataset's deposition;
   - choose the bucket: its own while unpublished; if already published,
     a **new version** first (a file written into a published record's
     bucket is accepted but never appears on it);
   - stream the file from disk to `<bucket>/<filename>` (percent-encoded;
     a file with the same name replaces it);
   - record it (new draft's id and bucket only once the file is in it,
     filename added once), delete the local file, release the disk
     space, delete the row;
   - trigger the publish if the dataset is public (a new version, or a
     first publish that waited for this file).

All chunk requests fit nginx's normal 20 MB body limit and default
timeouts: `/zenodo/uploads/` needs no special route.

The file is kept out of the kernel's page cache, after each chunk is
written and as the job reads it: on a 2 GB VM, a multi-GB file there
pushed CKAN, Postgres and Solr into swap and slowed the whole site.

### Cancelling

- **While the browser sends the chunks**: the progress bar's **Cancel**
  stops it and deletes what the server received (file and disk space).
- **Afterwards**, on the dataset page: **Remove**, for its editors and the
  user who started it. Waiting or failed: removed at once. Being sent to
  Zenodo: the job checks every 64 MB, stops, and removes it; the page
  says "Cancelled" meanwhile. An interrupted PUT leaves nothing in the
  Zenodo bucket, not even over a file of the same name (checked on the
  sandbox).
- **Purge of the dataset**: its uploads' files and disk space are removed
  too (their rows go with the dataset).

The send creates the dataset's Zenodo draft before the file goes in: a
draft left empty by a cancelled or given-up upload (never published, no
file, no other upload on its way) is deleted on Zenodo, with its row.

### When the send fails

The row goes `failed` with the reason; **the file is kept**, so nothing
has to be uploaded again.

- The dataset page shows it to its editors: "Sending to Zenodo failed:
  <reason>", with **Retry** and **Remove**.
- `ckan zenodo retry-pending` (cron, every 30 min) sends it again after
  30 min, up to 3 attempts in all; after the last one the page says no
  more automatic attempts are made, and the upload is removed a day
  later unless retried.
- A send still `sending` past the job's timeout (worker killed) is marked
  failed ("interrupted") by the same command, then follows the same
  rules.
- An upload left `receiving` for 2 h (browser closed mid-way) is removed
  by the same command.

### Older single-request upload - `POST /zenodo/upload/<dataset>`

Kept so a ckanext-fair3r older than the chunked upload still works: one
multipart request, the same checks, streamed to Zenodo within the
request (so limited to what fits in the web server's 50 s). Uses
nginx's `/zenodo/upload/` route (large body, no buffering).

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
8. **Previews**: for image, audio and video files, the matching core
   view (`image_view`, `audio_view`, `video_view`) is created or
   re-pointed at the file's direct content URL on Zenodo for this version
   (`/api/records/<record id>/files/<name>/content`). PDFs get none -
   Zenodo forbids embedding them - and the `pdf_view` CKAN adds by
   default is removed; "Go to resource" opens Zenodo's own preview.
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
exists in the draft. Deleting its last file deletes the whole draft and
its row (unless another upload is on its way): an empty draft would
otherwise stay on the shared account for good, for instance once the
editor stores the file in CKAN instead. The next upload creates a new
draft.

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
| Purge (sysadmin) | Same cleanup, for anything a delete didn't already remove; uploads not yet on Zenodo are dropped (file and disk space). | Nothing deleted (a published record can't be); a pending new-version draft is discarded; message: the files stay public on Zenodo. |
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
