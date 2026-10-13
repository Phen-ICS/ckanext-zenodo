"""Pure unit test (no ckan import, no DB) - stage 0 scaffolding only.
Later stages add real coverage (zenodo_client, metadata_mapper, model,
blueprint, publish_guard) as each is implemented."""


def test_plugin_module_importable():
    from ckanext.zenodo import plugin

    assert hasattr(plugin, 'ZenodoPlugin')
