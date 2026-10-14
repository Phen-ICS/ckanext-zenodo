![CKAN](https://img.shields.io/badge/CKAN-2.12.0-orange)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

# ckanext-zenodo

Publishes large or non-tabular dataset files to [Zenodo](https://zenodo.org/)
(sandbox during development, production later) instead of CKAN storage,
mapping the dataset's FDF metadata into the Zenodo deposition. The CKAN
dataset stays published in the FAIR3R catalogue (full FDF metadata,
searchable) with a link to the Zenodo DOI for the actual data, instead of
carrying the file itself.

## Status

Work in progress, built in staged merge requests. See the implementation
plan for the full design (file flow, data model, metadata mapping,
anti-abuse hardening, staged delivery order) - ask in the project chat for
the current plan reference if you don't have it to hand.

Routing rule (once implemented): a file goes to Zenodo instead of CKAN
storage when it's larger than `ckan.max_resource_size`, or its format isn't
one `ckanext.xloader.formats` can read - or whenever the user explicitly
asks for Zenodo regardless of size/format.

## Relationship to ckanext-fair3r

Not a declared package dependency (not in `pyproject.toml`), but FAIR3R is
this plugin's reason to exist, at two different levels:

- **Soft Python dependency** - `lib/publish_guard.py` imports
  `ckanext.fair3r.lib.fdf.datacite_converter.DataCiteConverter` inside a
  `try`/`except ImportError` to map a dataset's FDF metadata into the
  Zenodo deposition's description/creators/keywords/etc. Same pattern
  `ckanext-citations`' `helpers.py` already uses for
  `completeness_for_package`. Without `ckanext-fair3r` installed (or on a
  dataset with no `fdf_output_json` extra), a deposition still gets
  published - just with minimal metadata (title and the submitting user's
  name only), never a hard failure.
- **Operational dependency, not a code one** - the actual trigger for
  uploading to this plugin's `/zenodo/upload/<package>` endpoint lives
  entirely in `ckanext-fair3r`'s own `resource-upload.js` (the
  size/format routing decision and the "force Zenodo" checkbox). Without
  `ckanext-fair3r` installed *and enabled*, nothing in the normal CKAN UI
  ever calls this plugin's upload endpoint at all - it would need a
  hand-crafted request. See the implementation plan's staged delivery
  order for exactly where that wiring lands.

In short: install this plugin on its own and it runs (migrations apply,
the blueprint responds, tests pass), but it's only actually useful
alongside a FAIR3R deployment that also runs `ckanext-fair3r`.

## Sandbox smoke test findings (Stage 8)

Confirmed empirically against the real `sandbox.zenodo.org` (the current,
InvenioRDM-based Zenodo) - two things the implementation plan flagged as
unverified assumptions, now settled:

- **`metadata.creators` is required to publish**, even though updating a
  deposition's metadata without it is accepted - omitting it makes
  `publish_deposition` fail every time with a 400. `metadata_mapper.py`'s
  `datacite_to_zenodo` now always sets `creators`, falling back to the
  submitter's name when the DataCite metadata has none (e.g. no
  `ckanext-fair3r`, or no FDF metadata yet).
- **Setting `metadata.communities` does not associate the record with
  that community by itself** - it only opens a pending
  `community-inclusion` request against the record, which stays
  unaccepted (and invisible under the community) until explicitly
  accepted, even for the account that owns the community. `publish_guard.py`
  now does this automatically right after a successful publish
  (`_accept_pending_community_inclusion`) - best-effort: a failure here
  is logged but never undoes the publish (the DOI is already minted by
  that point).
- The description HTML sanitizer runs **on write**, not just on display,
  and its real allow-list is now documented directly above
  `build_zenodo_description_html` in `metadata_mapper.py` (confirmed by
  round-tripping a probe deposition through every plausible tag and
  reading back what Zenodo actually stored).

## Tests

```shell
docker exec -u ckan -it ckan-app pytest --ckan-ini=/plugins/ckanext-zenodo/test.ini /plugins/ckanext-zenodo/ckanext/zenodo/tests
```

## License

Released under the GNU Affero General Public License (AGPL) v3.0. See the
file `LICENSE` for details.
