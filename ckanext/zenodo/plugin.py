from ckan import plugins
from ckan.plugins import toolkit

from ckanext.zenodo import model
from ckanext.zenodo.blueprints.zenodo_upload import zenodo_upload
from ckanext.zenodo.lib.auth import resource_delete as zenodo_resource_delete_auth
from ckanext.zenodo.lib.delete_zenodo_file import maybe_delete_published_file
from ckanext.zenodo.lib.helpers import (
    zenodo_notice_for_package,
    zenodo_pending_deposition_for_package,
)
from ckanext.zenodo.lib.publish_guard import maybe_publish_pending_deposition


class ZenodoPlugin(plugins.SingletonPlugin):
    """Publishes large or non-tabular dataset files to Zenodo (sandbox or
    production) instead of CKAN storage, mapping FDF metadata into the
    Zenodo deposition. See the project's implementation plan for the full
    staged design (Zenodo client, metadata mapper, deposition model,
    upload blueprint, draft->active publish hook, resource-upload.js
    wiring).
    """

    plugins.implements(plugins.IAuthFunctions)
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.IPackageController, inherit=True)
    plugins.implements(plugins.IResourceController, inherit=True)
    plugins.implements(plugins.ITemplateHelpers)

    # IAuthFunctions

    def get_auth_functions(self):
        return {'resource_delete': zenodo_resource_delete_auth}

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

    # ITemplateHelpers

    def get_helpers(self):
        return {
            'zenodo_pending_deposition_for_package': zenodo_pending_deposition_for_package,
            'zenodo_notice_for_package': zenodo_notice_for_package,
        }

    # IPackageController

    def after_dataset_update(self, context, pkg_dict):
        """Independent of ckanext-fair3r's own after_dataset_update
        (draft_guard.py) - CKAN dispatches this hook to every plugin that
        implements IPackageController, so there's no need (and it would
        be the wrong ownership) to bolt this onto fair3r's hook. See the
        implementation plan's reasoning for keeping these separate."""
        maybe_publish_pending_deposition(pkg_dict)
        return pkg_dict

    # IResourceController

    def before_resource_delete(self, context, resource, resources):
        """Reacts to CKAN's own, already-existing "delete resource"
        button/action - see lib/delete_zenodo_file.py's own docstring
        for why this is a deferred RQ job rather than anything done
        synchronously here."""
        maybe_delete_published_file(context, resource, resources)
