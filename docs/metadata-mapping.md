# Metadata mapping

The Zenodo metadata is built at each publish by
`publish_guard._publish_deposition` and `metadata_mapper.datacite_to_zenodo`.
The source is the dataset's FDF form (`fdf_output_json` extra), converted
by ckanext-fair3r's `DataCiteConverter.fdf_to_datacite()` - the same
DataCite-shaped metadata the dataset's DataCite DOI is built from. Without
ckanext-fair3r, or without FDF metadata, a minimal record is published
(title, submitter as creator, tags, licence).

## Fields

| Zenodo field | Source | Rule |
|---|---|---|
| `title` | CKAN dataset title (name if empty) | |
| `upload_type` | DataCite `types.resourceTypeGeneral` | `Dataset`->`dataset`, `Software`->`software`, `Image`->`image`, `Text`->`publication`; anything else `other` |
| `creators` | DataCite `creators` | `name`; `affiliation` (several joined with `; `); `orcid` from an ORCID name identifier, `https://orcid.org/` prefix removed. **No creators: the submitter** (Zenodo refuses to publish a record without creators) |
| `contributors` | DataCite `contributors` | `name`, `affiliation`, `type` kept if it's one of Zenodo's contributor types, otherwise `Other` (logged) |
| `keywords` | CKAN dataset tags | Tag names as-is; FDF subjects go in the description instead |
| `related_identifiers` | DataCite `relatedIdentifiers` + the dataset's DataCite DOI | Relation types mapped to Zenodo's (table below), unknown ones become `references` (logged). The dataset's own DOI is added as `isPartOf` if ckanext-doi has minted it and it isn't already listed |
| `access_right` | CKAN `private` | Always `open` in practice: publishing only happens once the dataset is public |
| `license` | CKAN `license_id` | Passed as-is |
| `communities` | `ckanext.zenodo.community_identifier` | |
| `description` | Several sources | HTML, below |

### Relation types

DataCite -> Zenodo: `IsCitedBy`->`isCitedBy`, `Cites`->`cites`,
`IsSupplementTo`->`isSupplementTo`, `IsSupplementedBy`->`isSupplementedBy`,
`IsNewVersionOf`->`isNewVersionOf`, `IsPreviousVersionOf`->`isPreviousVersionOf`,
`IsPartOf`->`isPartOf`, `HasPart`->`hasPart`, `Compiles`->`compiles`,
`IsCompiledBy`->`isCompiledBy`, `IsIdenticalTo`->`isIdenticalTo`,
`IsReferencedBy`->`isReferencedBy`, `References`->`references`,
`IsDocumentedBy`->`isDocumentedBy`, `Documents`->`documents`,
`IsDerivedFrom`->`isDerivedFrom`, `IsSourceOf`->`isSourceOf`,
`Requires`->`requires`, `IsRequiredBy`->`isRequiredBy`,
`IsObsoletedBy`->`isObsoletedBy`, `Obsoletes`->`obsoletes`.
Anything else (e.g. `IsContinuedBy`, `HasMetadata`, `IsVersionOf`):
`references`.

## Description

Zenodo keeps a filtered subset of HTML. The description uses only
paragraphs, bold, lists, links and emoji (as icons), in this order:

1. **📤 Submitted via the FAIR3R shared account on behalf of:** the real
   person - the dataset creator's CKAN display name, else the dataset's
   `author`, else "a FAIR3R user". Every record is deposited by the same
   shared account, so this is the only place naming who actually
   submitted it.
2. **🔗 FAIR3R dataset:** link to the dataset's DataCite DOI (if minted),
   through ckanext-doi's resolver - DataCite's test resolver in test
   mode, `doi.org` in production.
3. The abstract (DataCite description of type `Abstract`).
4. **🧑‍🔬 Creators:** the creators' names.
5. The FDF subjects as a "Label: value" list. Known annotation schemes
   get a readable label (table below); any other scheme is turned into a
   label from its name (`treatmentProtocol` -> "Treatment Protocol").
   Duplicates (same label and value, case-insensitive) appear once - a
   gene cross-referenced in five databases gives one "Gene: Apoe" line.
6. **🏷️ Keywords:** the CKAN tags.
7. **📅 Publication year.**

| Subject scheme | Label |
|---|---|
| `NCBITaxon` | Organism |
| `PATO` | Sex |
| `speciesBackground`, `Strain` | Strain |
| `geneSymbol`, `geneName`, `geneAccessionId`, `NCBI Gene`, `MGI Gene`, `Ensembl Gene` | Gene |
| `geneChromosomeLocation` | Chromosome location |
| `geneMutationType` | Mutation type |
| `alleleSymbol`, `alleleAccessionId`, `MGI Allele`, `Alliance Allele` | Allele |
| `ChEBI`, `treatmentName` | Treatment |
| `treatmentProtocol` | Treatment protocol |
| `treatmentDesign` | Treatment design |
| `MONDO`, `DOID` | Disease model |
| `UBERON` | Tissue / organ |
| `EFO` | Diet |

## When it's sent

At every publish: the first one, and every new version (file added,
replaced or deleted). Metadata edited in CKAN while nothing is pending is
**not** pushed to Zenodo by itself; it reaches Zenodo with the next
version.
