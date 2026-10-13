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

## Tests

```shell
docker exec -u ckan -it ckan-app pytest --ckan-ini=/plugins/ckanext-zenodo/test.ini /plugins/ckanext-zenodo/ckanext/zenodo/tests
```

## License

Released under the GNU Affero General Public License (AGPL) v3.0. See the
file `LICENSE` for details.
