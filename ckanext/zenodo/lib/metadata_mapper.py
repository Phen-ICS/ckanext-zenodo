"""Maps a `DataCiteConverter.fdf_to_datacite()`-shaped dict (see
ckanext-fair3r/ckanext/fair3r/lib/fdf/datacite_converter.py) to Zenodo's own
deposition metadata schema. No `ckan` import - same reasoning as
zenodo_client.py: every value this needs that isn't already inside the
DataCite dict (the dataset's real title, who's actually submitting via the
shared account, the target community, access/license) is passed in
explicitly by the caller rather than read from CKAN config here.

Vocabulary mismatches between DataCite and Zenodo are real and handled
explicitly below (contributor types, relation types) rather than assumed
to line up - see each map's own comment for what's covered and what falls
back to a generic value.
"""

import logging

log = logging.getLogger(__name__)

# Zenodo's contributor "type" vocabulary is almost identical to DataCite's
# contributorType controlled list - this is a whitelist, not a rename
# table: anything not in it (including an empty/missing contributorType)
# falls back to "Other".
ZENODO_CONTRIBUTOR_TYPES = {
    'ContactPerson',
    'DataCollector',
    'DataCurator',
    'DataManager',
    'Distributor',
    'Editor',
    'HostingInstitution',
    'Producer',
    'ProjectLeader',
    'ProjectManager',
    'ProjectMember',
    'RegistrationAgency',
    'RegistrationAuthority',
    'RelatedPerson',
    'Researcher',
    'ResearchGroup',
    'RightsHolder',
    'Sponsor',
    'Supervisor',
    'WorkPackageLeader',
    'Other',
}

# DataCite's relationType vocabulary (see datacite_converter.py's own
# VALID_RELATION_TYPES, ~32 values, PascalCase) is richer than Zenodo's
# `relation` vocabulary (~22 values, lowerCamelCase) - only the ones with a
# direct equivalent are listed; anything else (including DataCite types
# with no Zenodo counterpart at all, e.g. IsContinuedBy/HasMetadata/
# IsVersionOf) falls back to "references", Zenodo's closest "generic
# link" relation - same fallback-to-a-safe-default idea already used by
# datacite_converter.py itself for an invalid DataCite relationType.
DATACITE_TO_ZENODO_RELATION = {
    'IsCitedBy': 'isCitedBy',
    'Cites': 'cites',
    'IsSupplementTo': 'isSupplementTo',
    'IsSupplementedBy': 'isSupplementedBy',
    'IsNewVersionOf': 'isNewVersionOf',
    'IsPreviousVersionOf': 'isPreviousVersionOf',
    'IsPartOf': 'isPartOf',
    'HasPart': 'hasPart',
    'Compiles': 'compiles',
    'IsCompiledBy': 'isCompiledBy',
    'IsIdenticalTo': 'isIdenticalTo',
    'IsReferencedBy': 'isReferencedBy',
    'References': 'references',
    'IsDocumentedBy': 'isDocumentedBy',
    'Documents': 'documents',
    'IsDerivedFrom': 'isDerivedFrom',
    'IsSourceOf': 'isSourceOf',
    'Requires': 'requires',
    'IsRequiredBy': 'isRequiredBy',
    'IsObsoletedBy': 'isObsoletedBy',
    'Obsoletes': 'obsoletes',
}
ZENODO_RELATION_FALLBACK = 'references'

# DataCite's resourceTypeGeneral is a much richer, finer-grained vocabulary
# than Zenodo's upload_type - only the handful FAIR3R is actually likely to
# produce are mapped; anything else falls back to "other".
RESOURCE_TYPE_GENERAL_TO_UPLOAD_TYPE = {
    'Dataset': 'dataset',
    'Software': 'software',
    'Image': 'image',
    'Text': 'publication',
}
ZENODO_UPLOAD_TYPE_FALLBACK = 'other'

# Ontology/vocabulary schemes datacite_converter.py itself already
# recognises for the FDF biological-annotation subjects (see its own
# _collect_subject_values_by_scheme calls) - appended to a keyword only for
# these, to keep Zenodo's flat keyword list readable rather than cluttered
# with every arbitrary subjectScheme value that might appear.
RECOGNIZED_SUBJECT_SCHEMES = {'NCBITaxon', 'ChEBI', 'DOID', 'UBERON', 'EFO'}

_ORCID_SCHEME = 'ORCID'
_ORCID_URL_PREFIX = 'https://orcid.org/'


def _strip_orcid_url(identifier):
    if identifier.startswith(_ORCID_URL_PREFIX):
        return identifier[len(_ORCID_URL_PREFIX) :]
    return identifier


def _creator_orcid(creator):
    for name_id in creator.get('nameIdentifiers') or []:
        if (name_id.get('nameIdentifierScheme') or '').upper() == _ORCID_SCHEME:
            value = (name_id.get('nameIdentifier') or '').strip()
            if value:
                return _strip_orcid_url(value)
    return None


def _joined_affiliation(entry):
    affiliations = [
        a.get('affiliation', '').strip()
        for a in entry.get('affiliation') or []
        if a.get('affiliation')
    ]
    return '; '.join(affiliations) if affiliations else None


def map_creator(creator):
    """DataCite creator dict -> Zenodo creators[] entry
    ({name, affiliation?, orcid?})."""
    mapped = {'name': creator.get('name', '')}
    affiliation = _joined_affiliation(creator)
    if affiliation:
        mapped['affiliation'] = affiliation
    orcid = _creator_orcid(creator)
    if orcid:
        mapped['orcid'] = orcid
    return mapped


def map_contributor(contributor):
    """DataCite contributor dict -> Zenodo contributors[] entry
    ({name, affiliation?, type})."""
    contributor_type = contributor.get('contributorType') or ''
    if contributor_type not in ZENODO_CONTRIBUTOR_TYPES:
        if contributor_type:
            log.warning(
                "Unmapped DataCite contributorType '%s' - using 'Other'",
                contributor_type,
            )
        contributor_type = 'Other'

    mapped = {'name': contributor.get('name', ''), 'type': contributor_type}
    affiliation = _joined_affiliation(contributor)
    if affiliation:
        mapped['affiliation'] = affiliation
    return mapped


def map_keywords(subjects):
    """DataCite subjects[] -> Zenodo's flat keywords[] list of strings."""
    keywords = []
    for subject in subjects or []:
        text = (subject.get('subject') or '').strip()
        if not text:
            continue
        scheme = subject.get('subjectScheme')
        if scheme in RECOGNIZED_SUBJECT_SCHEMES:
            keywords.append(f'{text} ({scheme})')
        else:
            keywords.append(text)
    return keywords


def map_related_identifiers(related_identifiers):
    """DataCite relatedIdentifiers[] -> Zenodo related_identifiers[]
    ({relation, identifier}). Zenodo's own `resource_type` for the related
    item is omitted - datacite_converter.py's output has no source field
    for it at all."""
    mapped = []
    for item in related_identifiers or []:
        identifier = (item.get('relatedIdentifier') or '').strip()
        if not identifier:
            continue
        relation_type = item.get('relationType') or ''
        relation = DATACITE_TO_ZENODO_RELATION.get(relation_type)
        if relation is None:
            log.warning(
                "Unmapped DataCite relationType '%s' - using '%s'",
                relation_type,
                ZENODO_RELATION_FALLBACK,
            )
            relation = ZENODO_RELATION_FALLBACK
        mapped.append({'relation': relation, 'identifier': identifier})
    return mapped


def map_upload_type(types):
    """DataCite types.resourceTypeGeneral -> Zenodo upload_type."""
    resource_type_general = (types or {}).get('resourceTypeGeneral')
    return RESOURCE_TYPE_GENERAL_TO_UPLOAD_TYPE.get(
        resource_type_general, ZENODO_UPLOAD_TYPE_FALLBACK
    )


# Zenodo sanitizes the description field through its own allow-list on
# write (not just on display) - confirmed empirically against the real
# sandbox during the Stage 8 smoke test by round-tripping a probe
# deposition through every tag this module might plausibly want and
# reading back what the API actually stored:
#   kept as-is: h1-h3, p, br, a[href] (but not [style]), span (tag kept,
#     any style/class-less attrs stripped), div[class] (class attribute
#     survives - unlike span's), ul/ol/li, table/tbody/tr/td, blockquote,
#     code, pre, sub, sup, u, strong, b, em
#   tag stripped, text content kept: script, s (strikethrough)
#   stripped entirely (tag and content both gone): img, hr
# Built from only the safest, smallest subset of the confirmed-safe list -
# headings, bold, an unordered list, plain Unicode emoji as "icons" instead
# of <img>/inline CSS, since the latter is confirmed stripped anyway.
def build_zenodo_description_html(datacite_metadata, submitter_display_name):
    submitted_on_behalf_of = (
        '<p><strong>📤 Submitted via the FAIR3R shared account on behalf of:'
        f'</strong> {submitter_display_name}</p>'
    )
    lines = [submitted_on_behalf_of]

    descriptions = datacite_metadata.get('descriptions') or []
    abstract = next(
        (
            d.get('description')
            for d in descriptions
            if d.get('descriptionType') == 'Abstract'
        ),
        None,
    )
    if abstract:
        lines.append(f'<p>{abstract}</p>')

    creators = datacite_metadata.get('creators') or []
    if creators:
        names = ', '.join(c.get('name', '') for c in creators if c.get('name'))
        if names:
            lines.append(f'<p><strong>🧑‍🔬 Creators:</strong> {names}</p>')

    keywords = map_keywords(datacite_metadata.get('subjects'))
    if keywords:
        items = ''.join(f'<li>{kw}</li>' for kw in keywords)
        lines.append(f'<p><strong>🏷️ Keywords:</strong></p><ul>{items}</ul>')

    publication_year = datacite_metadata.get('publicationYear')
    if publication_year:
        lines.append(f'<p><strong>📅 Publication year:</strong> {publication_year}</p>')

    return '\n'.join(lines)


def datacite_to_zenodo(
    datacite_metadata,
    *,
    title,
    submitter_display_name,
    community_identifier,
    access_right,
    license_id=None,
):
    """Build a Zenodo deposition metadata dict from a
    `DataCiteConverter.fdf_to_datacite()`-shaped dict plus the handful of
    values that dict doesn't carry (the dataset's real CKAN title, who's
    actually submitting, the target community, access/license - all
    derived from the CKAN package or plugin config by the caller, not
    re-derived here)."""
    metadata = {
        'title': title,
        'description': build_zenodo_description_html(
            datacite_metadata, submitter_display_name
        ),
        'upload_type': map_upload_type(datacite_metadata.get('types')),
        'access_right': access_right,
        'communities': [{'identifier': community_identifier}],
    }

    creators = [map_creator(c) for c in datacite_metadata.get('creators') or []]
    # Confirmed empirically against the real sandbox (Stage 8 smoke test):
    # Zenodo's PUT metadata accepts a deposition with no creators at all,
    # but then rejects *publishing* it with a 400 ("Missing data for
    # required field") - a dataset with no FDF creators (or fair3r not
    # installed, see publish_guard.py's _extract_datacite_metadata) would
    # otherwise fail to publish forever. Falls back to whoever is actually
    # submitting, same reasoning as the "submitted on behalf of" line in
    # the description above - there's always at least a name to put here.
    metadata['creators'] = creators or [{'name': submitter_display_name}]

    contributors = [
        map_contributor(c) for c in datacite_metadata.get('contributors') or []
    ]
    if contributors:
        metadata['contributors'] = contributors

    keywords = map_keywords(datacite_metadata.get('subjects'))
    if keywords:
        metadata['keywords'] = keywords

    related_identifiers = map_related_identifiers(
        datacite_metadata.get('relatedIdentifiers')
    )
    if related_identifiers:
        metadata['related_identifiers'] = related_identifiers

    if license_id:
        metadata['license'] = license_id

    return metadata
