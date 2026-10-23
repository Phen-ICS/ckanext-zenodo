"""The built-in upload option on CKAN's "new resource" form (for sites
without ckanext-fair3r). --ckan-ini=/plugins/ckanext-zenodo/test.ini
required (conftest.py guard); test.ini has no fair3r, which is the case
this feature exists for."""

import json

import pytest
from ckan import plugins
from ckan.tests import factories

from ckanext.zenodo.lib.helpers import zenodo_standalone_upload_config

pytestmark = pytest.mark.usefixtures('clean_db', 'zenodo_tables')


@pytest.mark.ckan_config('ckan.max_resource_size', '5')
@pytest.mark.ckan_config('ckanext.xloader.formats', 'csv tsv')
def test_config_is_the_routing_config():
    config = json.loads(zenodo_standalone_upload_config())

    assert config['max_resource_size_bytes'] == 5 * 1024 * 1024
    assert config['xloader_formats'] == ['csv', 'tsv']


@pytest.mark.ckan_config('ckanext.zenodo.sandbox_access_token', '')
def test_no_config_without_a_token():
    assert zenodo_standalone_upload_config() is None


def test_no_config_when_fair3r_is_enabled(monkeypatch):
    # fair3r's own script drives the form then: two scripts must never
    # intercept the same submit.
    monkeypatch.setattr(plugins, 'plugin_loaded', lambda name: name == 'fair3r')

    assert zenodo_standalone_upload_config() is None


def _get(app, url, user):
    response = app.get(url, headers={'Authorization': user['token']})
    return response.get_data(as_text=True)


def test_new_resource_form_has_the_zenodo_option(app):
    user = factories.SysadminWithToken()
    dataset = factories.Dataset(owner_org=factories.Organization()['id'])

    html = _get(app, f'/dataset/{dataset["name"]}/resource/new', user)

    assert 'id="zenodo-upload-option"' in html
    # Uploads in chunks to /zenodo/uploads/<dataset> (zenodo-chunked-upload.js).
    assert f'data-package-name="{dataset["name"]}"' in html
    assert f'data-package-url="/dataset/{dataset["name"]}"' in html
    assert '_upload.js' in html


def test_new_resource_form_has_no_option_when_fair3r_is_enabled(app, monkeypatch):
    user = factories.SysadminWithToken()
    dataset = factories.Dataset(owner_org=factories.Organization()['id'])
    monkeypatch.setattr(plugins, 'plugin_loaded', lambda name: name == 'fair3r')

    html = _get(app, f'/dataset/{dataset["name"]}/resource/new', user)

    assert 'zenodo-upload-option' not in html


@pytest.mark.ckan_config('ckanext.zenodo.sandbox_access_token', '')
def test_new_resource_form_has_no_option_without_a_token(app):
    user = factories.SysadminWithToken()
    dataset = factories.Dataset(owner_org=factories.Organization()['id'])

    html = _get(app, f'/dataset/{dataset["name"]}/resource/new', user)

    assert 'zenodo-upload-option' not in html


def test_edit_resource_form_has_no_option(app):
    # Editing an existing resource can't move it to Zenodo.
    user = factories.SysadminWithToken()
    dataset = factories.Dataset(owner_org=factories.Organization()['id'])
    resource = factories.Resource(package_id=dataset['id'])

    html = _get(app, f'/dataset/{dataset["name"]}/resource/{resource["id"]}/edit', user)

    assert 'zenodo-upload-option' not in html
