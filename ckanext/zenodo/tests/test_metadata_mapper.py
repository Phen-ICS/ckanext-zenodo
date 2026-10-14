"""Pure unit tests - no ckan import, no DB, no real network. One test per
mapping row from the implementation plan, plus a couple of fallback cases
and an end-to-end sanity check."""

from ckanext.zenodo.lib import metadata_mapper as mm


def test_map_creator_with_orcid_and_affiliation():
    creator = {
        'name': 'Smith, John',
        'nameIdentifiers': [
            {
                'nameIdentifier': 'https://orcid.org/0000-0001-2345-6789',
                'nameIdentifierScheme': 'ORCID',
            }
        ],
        'affiliation': [{'affiliation': 'University of Example'}],
    }

    mapped = mm.map_creator(creator)

    assert mapped == {
        'name': 'Smith, John',
        'affiliation': 'University of Example',
        'orcid': '0000-0001-2345-6789',
    }


def test_map_creator_without_orcid_or_affiliation():
    assert mm.map_creator({'name': 'Doe, Jane'}) == {'name': 'Doe, Jane'}


def test_map_creator_multiple_affiliations_joined():
    creator = {
        'name': 'Smith, John',
        'affiliation': [{'affiliation': 'Org A'}, {'affiliation': 'Org B'}],
    }

    assert mm.map_creator(creator)['affiliation'] == 'Org A; Org B'


def test_map_contributor_known_type_passes_through():
    contributor = {'name': 'Smith, John', 'contributorType': 'DataCurator'}

    assert mm.map_contributor(contributor) == {
        'name': 'Smith, John',
        'type': 'DataCurator',
    }


def test_map_contributor_unknown_type_falls_back_to_other():
    contributor = {'name': 'Smith, John', 'contributorType': 'NotARealType'}

    assert mm.map_contributor(contributor)['type'] == 'Other'


def test_map_contributor_missing_type_falls_back_to_other():
    assert mm.map_contributor({'name': 'Smith, John'})['type'] == 'Other'


def test_map_keywords_recognized_scheme_is_annotated():
    subjects = [{'subject': 'Mus musculus', 'subjectScheme': 'NCBITaxon'}]

    assert mm.map_keywords(subjects) == ['Mus musculus (NCBITaxon)']


def test_map_keywords_unrecognized_scheme_stays_plain():
    subjects = [{'subject': 'Gene: Apoe', 'subjectScheme': 'SomeOtherScheme'}]

    assert mm.map_keywords(subjects) == ['Gene: Apoe']


def test_map_keywords_no_scheme_stays_plain():
    assert mm.map_keywords([{'subject': 'free text'}]) == ['free text']


def test_map_keywords_skips_blank_subjects():
    assert mm.map_keywords([{'subject': '  '}, {'subject': 'ok'}]) == ['ok']


def test_map_related_identifiers_known_relation():
    related = [
        {
            'relatedIdentifier': '10.1234/example',
            'relatedIdentifierType': 'DOI',
            'relationType': 'IsSupplementTo',
        }
    ]

    assert mm.map_related_identifiers(related) == [
        {'relation': 'isSupplementTo', 'identifier': '10.1234/example'}
    ]


def test_map_related_identifiers_unmapped_relation_falls_back_to_references():
    related = [
        {'relatedIdentifier': '10.1234/example', 'relationType': 'IsDescribedBy'}
    ]

    assert mm.map_related_identifiers(related) == [
        {'relation': 'references', 'identifier': '10.1234/example'}
    ]


def test_map_related_identifiers_skips_blank_identifier():
    assert mm.map_related_identifiers([{'relatedIdentifier': ''}]) == []


def test_map_upload_type_dataset():
    assert mm.map_upload_type({'resourceTypeGeneral': 'Dataset'}) == 'dataset'


def test_map_upload_type_software():
    assert mm.map_upload_type({'resourceTypeGeneral': 'Software'}) == 'software'


def test_map_upload_type_unknown_falls_back_to_other():
    assert mm.map_upload_type({'resourceTypeGeneral': 'Collection'}) == 'other'


def test_map_upload_type_missing_types_falls_back_to_other():
    assert mm.map_upload_type(None) == 'other'


def test_build_description_html_includes_submitter_name():
    html = mm.build_zenodo_description_html({}, 'Jane Researcher')

    assert 'Jane Researcher' in html


def test_build_description_html_avoids_disallowed_tags():
    datacite_metadata = {
        'descriptions': [
            {'description': 'An abstract.', 'descriptionType': 'Abstract'}
        ],
        'creators': [{'name': 'Smith, John'}],
        'subjects': [{'subject': 'Mus musculus', 'subjectScheme': 'NCBITaxon'}],
        'publicationYear': '2026',
    }

    html = mm.build_zenodo_description_html(datacite_metadata, 'Jane Researcher')

    for disallowed in ('<img', '<script', 'style=', 'class='):
        assert disallowed not in html
    assert 'An abstract.' in html
    assert 'Smith, John' in html
    assert 'Mus musculus (NCBITaxon)' in html
    assert '2026' in html


def test_datacite_to_zenodo_end_to_end():
    datacite_metadata = {
        'creators': [
            {
                'name': 'Smith, John',
                'nameIdentifiers': [
                    {
                        'nameIdentifier': 'https://orcid.org/0000-0001-2345-6789',
                        'nameIdentifierScheme': 'ORCID',
                    }
                ],
            }
        ],
        'contributors': [{'name': 'Doe, Jane', 'contributorType': 'DataCurator'}],
        'subjects': [{'subject': 'Mus musculus', 'subjectScheme': 'NCBITaxon'}],
        'relatedIdentifiers': [
            {'relatedIdentifier': '10.1234/example', 'relationType': 'References'}
        ],
        'types': {'resourceTypeGeneral': 'Dataset'},
        'publicationYear': '2026',
    }

    result = mm.datacite_to_zenodo(
        datacite_metadata,
        title='A FAIR3R dataset',
        submitter_display_name='Jane Researcher',
        community_identifier='fair3r',
        access_right='open',
        license_id='cc-by-4.0',
    )

    assert result['title'] == 'A FAIR3R dataset'
    assert result['upload_type'] == 'dataset'
    assert result['access_right'] == 'open'
    assert result['communities'] == [{'identifier': 'fair3r'}]
    assert result['license'] == 'cc-by-4.0'
    assert result['creators'] == [
        {'name': 'Smith, John', 'orcid': '0000-0001-2345-6789'}
    ]
    assert result['contributors'] == [{'name': 'Doe, Jane', 'type': 'DataCurator'}]
    assert result['keywords'] == ['Mus musculus (NCBITaxon)']
    assert result['related_identifiers'] == [
        {'relation': 'references', 'identifier': '10.1234/example'}
    ]
    assert 'Jane Researcher' in result['description']


def test_datacite_to_zenodo_omits_empty_optional_fields():
    result = mm.datacite_to_zenodo(
        {},
        title='Untitled',
        submitter_display_name='Jane Researcher',
        community_identifier='fair3r',
        access_right='restricted',
    )

    # creators is never omitted - Zenodo rejects *publishing* a deposition
    # with none (confirmed against the real sandbox), so this falls back
    # to the submitter rather than being left out like the other optional
    # fields.
    assert result['creators'] == [{'name': 'Jane Researcher'}]
    assert 'contributors' not in result
    assert 'keywords' not in result
    assert 'related_identifiers' not in result
    assert 'license' not in result
