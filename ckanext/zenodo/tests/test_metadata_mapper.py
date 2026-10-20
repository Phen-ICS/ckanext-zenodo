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


def test_map_keywords_passes_through_ckan_tag_names():
    tags = [{'name': 'genomics'}, {'name': 'mouse-model'}]

    assert mm.map_keywords(tags) == ['genomics', 'mouse-model']


def test_map_keywords_skips_blank_tags():
    assert mm.map_keywords([{'name': '  '}, {'name': 'ok'}]) == ['ok']


def test_map_keywords_handles_no_tags():
    assert mm.map_keywords(None) == []
    assert mm.map_keywords([]) == []


def test_subject_label_known_scheme():
    assert mm._subject_label('NCBITaxon') == 'Organism'
    assert mm._subject_label('PATO') == 'Sex'
    assert mm._subject_label('speciesBackground') == 'Strain'
    assert mm._subject_label('alleleSymbol') == 'Allele'
    assert mm._subject_label('geneChromosomeLocation') == 'Chromosome location'
    assert mm._subject_label('geneMutationType') == 'Mutation type'
    assert mm._subject_label('DOID') == 'Disease model'
    assert mm._subject_label('UBERON') == 'Tissue / organ'
    assert mm._subject_label('EFO') == 'Diet'


def test_subject_label_unknown_scheme_is_humanized():
    # The FDF form is driven by a dynamic JSON schema - a scheme this
    # pipeline has never seen must still become a readable label, not a
    # plain unlabeled keyword (that bucket is reserved for CKAN tags).
    assert mm._subject_label('treatmentDuration') == 'Treatment Duration'
    assert mm._subject_label('someNewField') == 'Some New Field'


def test_subject_label_no_scheme_falls_back_to_other():
    assert mm._subject_label(None) == 'Other'
    assert mm._subject_label('') == 'Other'


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
    # NCBITaxon is a labeled subject (Organism) in the description - see
    # test_build_description_html_labels_organism_sex_and_strain - not
    # the plain "(NCBITaxon)"-suffixed keyword map_keywords() would
    # produce for Zenodo's own separate `keywords` field.
    assert '<strong>Organism:</strong> Mus musculus' in html
    assert '2026' in html


def test_build_description_html_labels_organism_sex_strain_and_mutation():
    # Confirmed against a real gene/allele FDF dataset's fdf_output_json.
    datacite_metadata = {
        'subjects': [
            {'subject': 'Mus musculus', 'subjectScheme': 'NCBITaxon'},
            {'subject': 'Female', 'subjectScheme': 'PATO'},
            {'subject': 'C57BL/6NJ', 'subjectScheme': 'speciesBackground'},
            {'subject': 'This is a mutation.', 'subjectScheme': 'geneMutationType'},
        ],
    }

    html = mm.build_zenodo_description_html(datacite_metadata, 'Jane Researcher')

    assert '<strong>Organism:</strong> Mus musculus' in html
    assert '<strong>Sex:</strong> Female' in html
    assert '<strong>Strain:</strong> C57BL/6NJ' in html
    assert '<strong>Mutation type:</strong> This is a mutation.' in html


def test_build_description_html_labels_unknown_scheme_via_fallback():
    # The FDF form is driven by a dynamic JSON schema - a scheme this
    # pipeline has never seen must still become a labeled pair, never a
    # plain unlabeled keyword (see test_subject_label_unknown_scheme_is_humanized).
    datacite_metadata = {
        'subjects': [{'subject': '5 days', 'subjectScheme': 'treatmentDuration'}],
    }

    html = mm.build_zenodo_description_html(datacite_metadata, 'Jane Researcher')

    assert '<strong>Treatment Duration:</strong> 5 days' in html
    assert 'Keywords' not in html


def test_build_description_html_dedupes_repeated_keywords():
    # A gene cross-referenced across several databases legitimately has
    # one subjects entry per database, all sharing the same plain text -
    # and, since geneAccessionId/"NCBI Gene"/"MGI Gene" all map to the
    # same "Gene" label, this also exercises the labeled side's dedup,
    # not just the plain-keywords one.
    datacite_metadata = {
        'subjects': [
            {'subject': 'Apoe', 'subjectScheme': 'geneAccessionId'},
            {'subject': 'Apoe', 'subjectScheme': 'NCBI Gene'},
            {'subject': 'apoe', 'subjectScheme': 'MGI Gene'},
            {'subject': 'Mus musculus', 'subjectScheme': 'NCBITaxon'},
            {'subject': 'mus musculus', 'subjectScheme': 'NCBITaxon'},
        ],
    }

    html = mm.build_zenodo_description_html(datacite_metadata, 'Jane Researcher')

    assert html.count('Apoe') == 1
    assert html.count('Mus musculus') == 1
    assert '<strong>Gene:</strong> Apoe' in html


def test_build_description_html_labels_gene_from_any_cross_reference():
    datacite_metadata = {
        'subjects': [
            {'subject': 'Apoe', 'subjectScheme': 'geneSymbol'},
            {'subject': 'Apoe', 'subjectScheme': 'geneAccessionId'},
            {'subject': 'Apoe', 'subjectScheme': 'NCBI Gene'},
            {'subject': 'Apoe', 'subjectScheme': 'MGI Gene'},
            {'subject': 'Apoe', 'subjectScheme': 'Ensembl Gene'},
        ],
    }

    html = mm.build_zenodo_description_html(datacite_metadata, 'Jane Researcher')

    assert html.count('Apoe') == 1
    assert '<strong>Gene:</strong> Apoe' in html
    assert 'Keywords' not in html


def test_build_description_html_keywords_section_comes_from_ckan_tags():
    # Not from subjects - those are the labeled pairs above. Passing no
    # tags at all means no Keywords section, even with subjects present.
    datacite_metadata = {
        'subjects': [{'subject': 'Mus musculus', 'subjectScheme': 'NCBITaxon'}],
    }

    without_tags = mm.build_zenodo_description_html(
        datacite_metadata, 'Jane Researcher'
    )
    assert 'Keywords' not in without_tags

    with_tags = mm.build_zenodo_description_html(
        datacite_metadata, 'Jane Researcher', tags=[{'name': 'genomics'}]
    )
    assert '<strong>🏷️ Keywords:</strong>' in with_tags
    assert '<li>genomics</li>' in with_tags


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
        tags=[{'name': 'genomics'}],
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
    # From the dataset's own CKAN tags, not the NCBITaxon subject above -
    # that one shows up as an "Organism:" labeled pair in the description
    # instead (see test_build_description_html_labels_organism_sex_strain_and_mutation).
    assert result['keywords'] == ['genomics']
    assert '<strong>Organism:</strong> Mus musculus' in result['description']
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


def test_datacite_to_zenodo_links_back_to_the_fair3r_dataset_doi():
    result = mm.datacite_to_zenodo(
        {},
        title='T',
        submitter_display_name='Jane',
        community_identifier='fair3r',
        access_right='open',
        source_doi='10.82999/fair3r.abc',
    )

    assert result['related_identifiers'] == [
        {'relation': 'isPartOf', 'identifier': '10.82999/fair3r.abc'}
    ]
    assert 'href="https://doi.org/10.82999/fair3r.abc"' in result['description']


def test_datacite_to_zenodo_does_not_duplicate_an_existing_source_doi():
    result = mm.datacite_to_zenodo(
        {
            'relatedIdentifiers': [
                {'relatedIdentifier': '10.82999/fair3r.abc', 'relationType': 'IsPartOf'}
            ]
        },
        title='T',
        submitter_display_name='Jane',
        community_identifier='fair3r',
        access_right='open',
        source_doi='10.82999/fair3r.abc',
    )

    assert len(result['related_identifiers']) == 1


def test_source_doi_link_can_point_at_another_resolver():
    # e.g. DataCite's test resolver in ckanext-doi's test mode
    result = mm.datacite_to_zenodo(
        {},
        title='T',
        submitter_display_name='Jane',
        community_identifier='fair3r',
        access_right='open',
        source_doi='10.83249/abc',
        source_doi_url='https://handle.test.datacite.org/10.83249/abc',
    )

    assert (
        'href="https://handle.test.datacite.org/10.83249/abc"' in result['description']
    )
    assert result['related_identifiers'] == [
        {'relation': 'isPartOf', 'identifier': '10.83249/abc'}
    ]
