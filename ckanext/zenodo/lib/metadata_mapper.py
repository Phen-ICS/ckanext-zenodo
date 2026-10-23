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
import re
from html import escape

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


def _humanize_scheme_label(scheme):
    """A readable label from a subjectScheme string, e.g.
    "treatmentProtocol" -> "Treatment Protocol". Only for when
    ckanext-fair3r can't label subjects from the FDF schema (see
    _split_labeled_subjects)."""
    if not scheme:
        return 'Other'
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', scheme)
    words = spaced.split()
    return ' '.join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


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


def map_keywords(tags):
    """CKAN dataset tags[] (as package_show returns them - a list of
    {'name': ..., ...} dicts) -> Zenodo's flat keywords[] list of
    strings.

    Deliberately NOT derived from FDF subjects: those are rich,
    structured biological annotations, and every one of them gets its
    own "Label: value" pair instead (see _split_labeled_subjects) -
    Zenodo's own `keywords` field is free-text search tags, which is
    exactly what the dataset's own CKAN tags already are, so this just
    passes them through rather than repurposing the subjects list for
    a second, different kind of field."""
    keywords = []
    for tag in tags or []:
        name = (tag.get('name') or tag.get('display_name') or '').strip()
        if name:
            keywords.append(name)
    return keywords


def _split_labeled_subjects(subjects, subject_lines=None):
    """Every FDF subject as a (label, value) pair. `subject_lines` is
    ckanext-fair3r's [(label, values)], labelled from the FDF schema's own
    display_mapping (see publish_guard.py) - no scheme list in this plugin,
    so a new ontology or form section needs no change here. Without it
    (fair3r missing or older), each DataCite subject goes under its
    humanized scheme name. Never a plain, unlabeled keyword - see
    map_keywords for why that bucket is the dataset's CKAN tags.

    Deduplicated by (label, value), case-insensitive, first kept."""
    if subject_lines is not None:
        pairs = [(label, value) for label, values in subject_lines for value in values]
    else:
        pairs = [
            (
                _humanize_scheme_label(subject.get('subjectScheme')),
                (subject.get('subject') or '').strip(),
            )
            for subject in subjects or []
        ]
    labeled = []
    seen = set()
    for label, text in pairs:
        key = (label, text.lower())
        if not text or key in seen:
            continue
        seen.add(key)
        labeled.append((label, text))
    return labeled


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
def build_zenodo_description_html(
    datacite_metadata,
    submitter_display_name,
    tags=None,
    source_doi=None,
    source_doi_url=None,
    subject_lines=None,
):
    """`tags` is the dataset's own CKAN tags (package_show's `tags`
    list), not part of the DataCite dict - passed in explicitly by the
    caller, same reasoning as submitter_display_name below (it's a
    CKAN-only concept this module has no other way to see).
    `source_doi` is the FAIR3R dataset's own DataCite DOI (ckanext-doi),
    linked so a reader landing on Zenodo can get back to the full
    catalogue record. `source_doi_url` is where that link points
    (defaults to doi.org - see publish_guard.py for the test-mode case).

    Every value is HTML-escaped: they come from users (display name, FDF
    form, tags), and Zenodo keeps links and formatting in what it publishes
    under the shared account."""
    submitted_on_behalf_of = (
        '<p><strong>📤 Submitted via the FAIR3R shared account on behalf of:'
        f'</strong> {escape(submitter_display_name)}</p>'
    )
    lines = [submitted_on_behalf_of]
    if source_doi:
        lines.append(
            '<p><strong>🔗 FAIR3R dataset:</strong> '
            f'<a href="{escape(source_doi_url or f"https://doi.org/{source_doi}")}">'
            f'{escape(source_doi)}</a></p>'
        )

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
        lines.append(f'<p>{escape(abstract)}</p>')

    creators = datacite_metadata.get('creators') or []
    if creators:
        names = ', '.join(c.get('name', '') for c in creators if c.get('name'))
        if names:
            lines.append(f'<p><strong>🧑‍🔬 Creators:</strong> {escape(names)}</p>')

    labeled_subjects = _split_labeled_subjects(
        datacite_metadata.get('subjects'), subject_lines
    )
    if labeled_subjects:
        items = ''.join(
            f'<li><strong>{escape(label)}:</strong> {escape(value)}</li>'
            for label, value in labeled_subjects
        )
        lines.append(f'<ul>{items}</ul>')

    keywords = map_keywords(tags)
    if keywords:
        items = ''.join(f'<li>{escape(kw)}</li>' for kw in keywords)
        lines.append(f'<p><strong>🏷️ Keywords:</strong></p><ul>{items}</ul>')

    publication_year = datacite_metadata.get('publicationYear')
    if publication_year:
        lines.append(
            f'<p><strong>📅 Publication year:</strong> {escape(str(publication_year))}</p>'
        )

    return '\n'.join(lines)


def datacite_to_zenodo(
    datacite_metadata,
    *,
    title,
    submitter_display_name,
    community_identifier,
    access_right,
    tags=None,
    license_id=None,
    source_doi=None,
    source_doi_url=None,
    subject_lines=None,
):
    """Build a Zenodo deposition metadata dict from a
    `DataCiteConverter.fdf_to_datacite()`-shaped dict plus the handful of
    values that dict doesn't carry (the dataset's real CKAN title, who's
    actually submitting, the target community, access/license, the
    dataset's own CKAN tags, its own DataCite DOI - all derived from the
    CKAN package or plugin config by the caller, not re-derived here)."""
    metadata = {
        'title': title,
        'description': build_zenodo_description_html(
            datacite_metadata,
            submitter_display_name,
            tags,
            source_doi,
            source_doi_url,
            subject_lines,
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

    keywords = map_keywords(tags)
    if keywords:
        metadata['keywords'] = keywords

    related_identifiers = map_related_identifiers(
        datacite_metadata.get('relatedIdentifiers')
    )
    # Machine-readable link back to the FAIR3R catalogue record: these
    # files are part of that dataset. Lets DataCite/Zenodo/OpenAlex
    # resolve the relation, not just a human reading the description.
    if source_doi and not any(
        r['identifier'] == source_doi for r in related_identifiers
    ):
        related_identifiers.append({'relation': 'isPartOf', 'identifier': source_doi})
    if related_identifiers:
        metadata['related_identifiers'] = related_identifiers

    if license_id:
        metadata['license'] = license_id

    return metadata
