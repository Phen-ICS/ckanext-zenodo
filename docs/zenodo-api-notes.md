# Zenodo API notes

Zenodo behaviour the design depends on, confirmed against
`sandbox.zenodo.org` (InvenioRDM-based Zenodo, with its legacy
`/api/deposit/depositions` API). Several points differ from what the
documentation suggests; re-check them here before changing the related
code.

## Depositions and files

- Files are uploaded with the **bucket API**: `PUT <links.bucket>/<name>`,
  streamed. Not the legacy multipart `/files` endpoint, meant for small
  files.
- **Same name = replace.** A second PUT under an existing name leaves one
  file, with the new content. (The legacy multipart endpoint refuses
  instead: 400 "File with key ... already exists".)
- **The name must be percent-encoded** as one path segment: left raw,
  `#` or `?` end the path (`data#1.csv` was stored as `data`). Encoded
  names come back unchanged.
- **Writing into a published record's bucket "succeeds" (200) but the
  file never appears on the record.** Adding a file after publication
  needs a new version (below).
- A deposition with **no files can't be published** ("mark it as
  metadata-only"); one with **no creators** is accepted as a draft but
  can't be published either (400, missing required field).
- A never-published deposition can be deleted
  (`DELETE /api/deposit/depositions/<id>`); a new-version draft is
  discarded (`POST .../actions/discard`). A published record can't be
  deleted through the API.

## Versions and DOIs

- `POST .../actions/newversion` creates a draft copying the latest
  version's files and metadata; the response already contains the
  draft's `links.bucket`.
- Each published version has its **own record id and DOI**. The
  **concept record id** (`conceptrecid`) and **concept DOI** are shared
  by all versions; `/records/<concept id>` redirects to the latest
  version, keeping its query string.
- `/records/<concept id>?preview_file=<name>` (name with `quote_plus`)
  opens the landing page on that file.
- Direct file content: `/api/records/<version record id>/files/<name>/content`
  - works without authentication, but only with a *version* id, not the
  concept id. The `links.download` returned by the legacy deposit API is
  stale (still points at the draft) and returns 404.
- All files of a version as a ZIP: `/api/records/<version record id>/files-archive`
  (404 with the concept id).
- Zenodo pages send `X-Frame-Options: sameorigin`: they can't be shown in
  an iframe. `<img>`, `<audio>`, `<video>` and `<object>` on the direct
  content URL work - hence previews through CKAN's own view plugins.

## Communities

- `metadata.communities` at publish time creates an inclusion request
  **only on a record's first publish**, never for later versions. The
  request is submitted explicitly on every publish
  (`POST /api/records/<id>/communities`); Zenodo answers 400 if one is
  already open or the record is already included - treated as success.
- The request is then **accepted** by the same account
  (`POST /api/requests/<id>/actions/accept`). This needs the account to
  be owner, manager or curator of the community; otherwise it fails with
  403 while the publish itself succeeded.
- `GET /api/me` gives the token's account id; `GET /api/communities/<slug>`
  works without authentication (404 if unknown);
  `GET /api/communities/<id>/members` lists `member.id` and `role`.

## Sandbox vs production

- Separate sites: accounts, tokens, communities and records don't
  carry over - a token only works on the site that issued it.
- Sandbox DOIs use DataCite's test prefix `10.5072` and don't resolve on
  doi.org - resource descriptions link to the sandbox record page
  instead.
- Unauthenticated API listing is capped (page size 25 without a token).

## Errors and rate limiting

`zenodo_client` retries 429, 5xx and network errors with exponential
backoff (4 attempts, from 1 s); other 4xx fail immediately; 404 is
returned as "not found". Uploads get a 600 s timeout, other calls 20 s.
