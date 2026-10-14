"""DB-backed only because reading ckan.config needs a loaded CKAN app -
--ckan-ini=/plugins/ckanext-zenodo/test.ini required (conftest.py guard).
No DB writes at all, just config reads."""

import pytest

from ckanext.zenodo.lib import zenodo_config


@pytest.mark.ckan_config('ckan.max_resource_size', '5')
@pytest.mark.ckan_config('ckanext.xloader.formats', 'csv application/csv')
def test_routing_config_dict_reflects_config():
    config = zenodo_config.routing_config_dict()

    assert config == {
        'max_resource_size_bytes': 5 * 1024 * 1024,
        'xloader_formats': ['csv', 'application/csv'],
    }


def test_sandbox_mode_defaults_to_true():
    assert zenodo_config.sandbox_mode() is True


@pytest.mark.ckan_config('ckanext.zenodo.test_mode', 'false')
def test_sandbox_mode_can_be_disabled():
    assert zenodo_config.sandbox_mode() is False


def test_access_token_raises_when_not_configured():
    with pytest.raises(RuntimeError):
        zenodo_config.access_token(
            sandbox=False
        )  # production token not set in test.ini


def test_access_token_sandbox_reads_test_ini_value():
    assert zenodo_config.access_token(sandbox=True) == 'test-sandbox-token'


def test_community_identifier_default():
    assert zenodo_config.community_identifier() == 'fair3r'
