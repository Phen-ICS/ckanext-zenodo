# ckanext-zenodo - specification

ckanext-zenodo stores a dataset's large or non-tabular files on
[Zenodo](https://zenodo.org/) instead of in CKAN's own storage, and
publishes them there - with a DOI, in the FAIR3R community - when the
dataset itself is made public. The dataset stays a normal, searchable CKAN
dataset; each Zenodo file shows up in it as a regular resource that links
to Zenodo.

| Document | Covers |
|---|---|
| [architecture.md](architecture.md) | Components, CKAN interfaces used, modules, data model, dependencies on other extensions |
| [workflows.md](workflows.md) | Every user-facing action, step by step: upload, publish, add/replace/delete a file, delete/purge/make private a dataset, download, failures and retries |
| [metadata-mapping.md](metadata-mapping.md) | How the FDF/DataCite metadata becomes Zenodo metadata, field by field |
| [security.md](security.md) | Who may do what, the anti-abuse limits, what stays secret, known limitations |
| [operations.md](operations.md) | Configuration reference, CLI commands, deployment, troubleshooting |
| [zenodo-api-notes.md](zenodo-api-notes.md) | Zenodo API behaviour confirmed against the sandbox, which the design depends on |

## In one paragraph

A user adds a file to a dataset. If the file is larger than
`ckan.max_resource_size`, in a format the data viewer can't read, or the
user switched "Zenodo" on, the browser sends it to this plugin instead of
CKAN, which streams it into a Zenodo **draft deposition** kept for that
dataset (one per dataset), using one shared Zenodo account. Nothing is
public yet. When the dataset becomes **active and public**, a background
job writes the metadata (converted from the FDF form), publishes the
deposition (Zenodo mints a DOI), files it into the community and creates
one CKAN **Link resource** per file. Later changes - adding, replacing or
deleting a file - publish a **new version** of the same Zenodo record;
CKAN resources point at the record's *concept* DOI, which always resolves
to the latest version.

## Vocabulary

| Term | Meaning |
|---|---|
| Deposition | A Zenodo record being edited (a draft). One per dataset, stored in `fair_zenodo_deposition`. |
| Record / version | A published deposition. Each publish creates a new version with its own record id and DOI. |
| Concept record / concept DOI | The id and DOI shared by every version of a record; they always resolve to the latest version. |
| Bucket | The deposition's file storage on Zenodo; files are PUT into it by name. |
| Community | A Zenodo collection (`fair3r`). A record enters it through an inclusion request that its curators accept. |
| Sandbox | `sandbox.zenodo.org`, Zenodo's test instance - separate accounts, tokens, communities and test DOIs (prefix `10.5072`). Used when `ckanext.zenodo.test_mode` is true. |
| Zenodo resource | A CKAN Link resource created by this plugin for one Zenodo file. |
| Pending file | A file on Zenodo that has no CKAN resource yet (the deposition isn't published). |
