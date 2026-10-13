from ckan import plugins
from ckan.plugins import toolkit


class ZenodoPlugin(plugins.SingletonPlugin):
    """Publishes large or non-tabular dataset files to Zenodo (sandbox or
    production) instead of CKAN storage, mapping FDF metadata into the
    Zenodo deposition. See the project's implementation plan for the full
    staged design (Zenodo client, metadata mapper, deposition model,
    upload blueprint, draft->active publish hook, resource-upload.js
    wiring) - this is deliberately a do-nothing scaffold for now (stage 0):
    later stages add implementations and declare the additional interfaces
    (IBlueprint, IPackageController) they need.
    """

    plugins.implements(plugins.IConfigurer)

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
