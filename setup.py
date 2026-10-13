#!/usr/bin/env python
import setuptools

if __name__ == '__main__':
    setuptools.setup(
        message_extractors={
            'ckanext': [
                ('**.py', 'python', None),
                ('**/templates/**.html', 'ckan', None),
            ],
        },
    )
