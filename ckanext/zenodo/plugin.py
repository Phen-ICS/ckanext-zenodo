from ckan import plugins
from ckan.plugins import toolkit

from ckanext.zenodo import model
from ckanext.zenodo.blueprints.zenodo_upload import zenodo_upload


class ZenodoPlugin(plugins.SingletonPlugin):
    """Publishes large or non-tabular dataset files to Zenodo (sandbox or
    production) instead of CKAN storage, mapping FDF metadata into the
    Zenodo deposition. See the project's implementation plan for the full
    staged design (Zenodo client, metadata mapper, deposition model,
    upload blueprint, draft->active publish hook, resource-upload.js
    wiring) - the publish hook (IPackageController) lands in a later
    stage.
    """

    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IBlueprint)

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
        # Touching model.* at load time registers the SQLAlchemy mappings
        # with ckan.model.meta before anything queries them - same pattern
        # ckanext-citations' plugin.py uses.
        _ = model

    # IBlueprint

    def get_blueprint(self):
        return [zenodo_upload]
