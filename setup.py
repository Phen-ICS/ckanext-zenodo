#!/usr/bin/env python
import setuptools

if __name__ == '__main__':
    setuptools.setup(
        message_extractors={
            'ckanext': [
                ('**.py', 'python', None),
                # translate("...") in assets/js, served to the browser by
                # CKAN's /api/i18n only if the .po lists a .js location.
                ('**.js', 'javascript', None),
                ('**/templates/**.html', 'ckan', None),
            ],
        },
    )
