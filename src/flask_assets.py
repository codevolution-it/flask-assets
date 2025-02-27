"""Integration of the ``webassets`` library with Flask."""

from __future__ import print_function

import logging
from os import path

try:
    from flask.globals import request_ctx, app_ctx
except ImportError:
    from flask import _request_ctx_stack, _app_ctx_stack
    request_ctx = _request_ctx_stack.top
    app_ctx = _app_ctx_stack.top
from flask import current_app, has_app_context, has_request_context
from flask.templating import render_template_string
# We want to expose Bundle via this module.
from webassets import Bundle
from webassets.env import (BaseEnvironment, ConfigStorage, Resolver,
                           env_options, url_prefix_join)
from webassets.filter import Filter, register_filter
from webassets.loaders import PythonLoader, YAMLLoader

__version__ = (2, 1, 1, 'dev')
# webassets core compatibility used in setup.py
__webassets_version__ = ('>=2.0', )

__all__ = (
    'Environment',
    'Bundle',
    'FlaskConfigStorage',
    'FlaskResolver',
    'Jinja2Filter',
)


class Jinja2Filter(Filter):
    """Will compile all source files as Jinja2 templates using the standard
    Flask contexts.
    """
    name = 'jinja2'
    max_debug_level = None

    def __init__(self, context=None):
        super(Jinja2Filter, self).__init__()
        self.context = context or {}

    def input(self, _in, out, source_path, output_path, **kw):
        out.write(render_template_string(_in.read(), **self.context))

# Override the built-in ``jinja2`` filter that ships with ``webassets``. This
# custom filter uses Flask's ``render_template_string`` function to provide all
# the standard Flask template context variables.
register_filter(Jinja2Filter)


class FlaskConfigStorage(ConfigStorage):
    """Uses the config object of a Flask app as the backend: either the app
    instance bound to the extension directly, or the current Flask app on
    the stack.

    Also provides per-application defaults for some values.

    Note that if no app is available, this config object is basically
    unusable - this is by design; this could also let the user set defaults
    by writing to a container not related to any app, which would be used
    as a fallback if a current app does not include a key. However, at least
    for now, I specifically made the choice to keep things simple and not
    allow global across-app defaults.
    """

    def __init__(self, *a, **kw):
        self._defaults = {}
        ConfigStorage.__init__(self, *a, **kw)

    def _transform_key(self, key):
        if key.lower() in env_options:
            return "ASSETS_%s" % key.upper()
        else:
            return key.upper()

    def setdefault(self, key, value):
        """We may not always be connected to an app, but we still need
        to provide a way to the base environment to set it's defaults.
        """
        try:
            super(FlaskConfigStorage, self).setdefault(key, value)
        except RuntimeError:
            self._defaults.__setitem__(key, value)

    def __contains__(self, key):
        return self._transform_key(key) in self.env._app.config

    def __getitem__(self, key):
        value = self._get_deprecated(key)
        if value:
            return value

        # First try the current app's config
        public_key = self._transform_key(key)
        if self.env._app:
            if public_key in self.env._app.config:
                return self.env._app.config[public_key]

        # Try a non-app specific default value
        if key in self._defaults:
            return self._defaults.__getitem__(key)

        # Finally try to use a default based on the current app
        deffunc = getattr(self, "_app_default_%s" % key, False)
        if deffunc:
            return deffunc()

        # We've run out of options
        raise KeyError()

    def __setitem__(self, key, value):
        if not self._set_deprecated(key, value):
            self.env._app.config[self._transform_key(key)] = value

    def __delitem__(self, key):
        del self.env._app.config[self._transform_key(key)]


def get_static_folder(app_or_blueprint):
    """Return the static folder of the given Flask app
    instance, or module/blueprint.

    In newer Flask versions this can be customized, in older
    ones (<=0.6) the folder is fixed.
    """
    if not hasattr(app_or_blueprint, 'static_folder'):
        # I believe this is for app objects in very old Flask
        # versions that did not support custom static folders.
        return path.join(app_or_blueprint.root_path, 'static')

    if not app_or_blueprint.has_static_folder:
        # Use an exception type here that is not hidden by spit_prefix.
        raise TypeError(('The referenced blueprint %s has no static '
                         'folder.') % app_or_blueprint)
    return app_or_blueprint.static_folder


class FlaskResolver(Resolver):
    """Adds support for Flask blueprints.

    This resolver is designed to use the Flask staticfile system to
    locate files, by looking at directory prefixes (``foo/bar.png``
    looks in the static folder of the ``foo`` blueprint. ``url_for``
    is used to generate urls to these files.

    This default behaviour changes when you start setting certain
    standard *webassets* path and url configuration values:

    If a :attr:`Environment.directory` is set, output files will
    always be written there, while source files still use the Flask
    system.

    If a :attr:`Environment.load_path` is set, it is used to look
    up source files, replacing the Flask system. Blueprint prefixes
    are no longer resolved.
    """

    def split_prefix(self, ctx, item):
        """See if ``item`` has blueprint prefix, return (directory, rel_path).
        """
        app = ctx._app
        try:
            if hasattr(app, 'blueprints'):
                blueprint, name = item.split('/', 1)
                directory = get_static_folder(app.blueprints[blueprint])
                endpoint = '%s.static' % blueprint
                item = name
            else:
                # Module support for Flask < 0.7
                module, name = item.split('/', 1)
                directory = get_static_folder(app.modules[module])
                endpoint = '%s.static' % module
                item = name
        except (ValueError, KeyError):
            directory = get_static_folder(app)
            endpoint = 'static'

        return directory, item, endpoint

    def use_webassets_system_for_output(self, ctx):
        return ctx.config.get('directory') is not None or \
               ctx.config.get('url') is not None

    def use_webassets_system_for_sources(self, ctx):
        return bool(ctx.load_path)

    def search_for_source(self, ctx, item):
        # If a load_path is set, use it instead of the Flask static system.
        #
        # Note: With only env.directory set, we don't go to default;
        # Setting env.directory only makes the output directory fixed.
        if self.use_webassets_system_for_sources(ctx):
            return Resolver.search_for_source(self, ctx, item)

        # Look in correct blueprint's directory
        directory, item, endpoint = self.split_prefix(ctx, item)
        try:
            return self.consider_single_directory(directory, item)
        except IOError:
            # XXX: Hack to make the tests pass, which are written to not
            # expect an IOError upon missing files. They need to be rewritten.
            return path.normpath(path.join(directory, item))

    def resolve_output_to_path(self, ctx, target, bundle):
        # If a directory/url pair is set, always use it for output files
        if self.use_webassets_system_for_output(ctx):
            return Resolver.resolve_output_to_path(self, ctx, target, bundle)

        # Allow targeting blueprint static folders
        directory, rel_path, endpoint = self.split_prefix(ctx, target)
        return path.normpath(path.join(directory, rel_path))

    def resolve_source_to_url(self, ctx, filepath, item):
        # If a load path is set, use it instead of the Flask static system.
        if self.use_webassets_system_for_sources(ctx):
            return super(FlaskResolver, self).resolve_source_to_url(ctx, filepath, item)

        return self.convert_item_to_flask_url(ctx, item, filepath)

    def resolve_output_to_url(self, ctx, target):
        # With a directory/url pair set, use it for output files.
        if self.use_webassets_system_for_output(ctx):
            return Resolver.resolve_output_to_url(self, ctx, target)

        # Otherwise, behaves like all other flask URLs.
        return self.convert_item_to_flask_url(ctx, target)

    def convert_item_to_flask_url(self, ctx, item, filepath=None):
        """Given a relative reference like `foo/bar.css`, returns
        the Flask static url. By doing so it takes into account
        blueprints, i.e. in the aformentioned example,
        ``foo`` may reference a blueprint.

        If an absolute path is given via ``filepath``, it will be
        used instead. This is needed because ``item`` may be a
        glob instruction that was resolved to multiple files.

        If app.config("FLASK_ASSETS_USE_S3") exists and is True
        then we import the url_for function from flask_s3,
        otherwise we import url_for from flask directly.

        If app.config("FLASK_ASSETS_USE_CDN") exists and is True
        then we import the url_for function from flask.
        """
        if ctx.environment._app.config.get("FLASK_ASSETS_USE_S3"):
            try:
                from flask_s3 import url_for
            except ImportError as e:
                print("You must have Flask S3 to use FLASK_ASSETS_USE_S3 option")
                raise e
        elif ctx.environment._app.config.get("FLASK_ASSETS_USE_CDN"):
            try:
                from flask_cdn import url_for
            except ImportError as e:
                print("You must have Flask CDN to use FLASK_ASSETS_USE_CDN option")
                raise e
        elif ctx.environment._app.config.get("FLASK_ASSETS_USE_AZURE"):
            try:
                from flask_azure_storage import url_for
            except ImportError as e:
                print("You must have Flask Azure Storage to use FLASK_ASSETS_USE_AZURE option")
                raise e
        else:
            from flask import url_for

        directory, rel_path, endpoint = self.split_prefix(ctx, item)

        if filepath is not None:
            filename = filepath[len(directory)+1:]
        else:
            filename = rel_path

        flask_ctx = None
        if not has_request_context():
            flask_ctx = ctx.environment._app.test_request_context()
            flask_ctx.push()
        try:
            url = url_for(endpoint, filename=filename)
            # In some cases, url will be an absolute url with a scheme and hostname.
            # (for example, when using werkzeug's host matching).
            # In general, url_for() will return a http url. During assets build, we
            # we don't know yet if the assets will be served over http, https or both.
            # Let's use // instead. url_for takes a _scheme argument, but only together
            # with external=True, which we do not want to force every time. Further,
            # this _scheme argument is not able to render // - it always forces a colon.
            if url and url.startswith('http:'):
                url = url[5:]
            return url
        finally:
            if flask_ctx:
                flask_ctx.pop()


class Environment(BaseEnvironment):
    """This object is used to hold a collection of bundles and configuration.

    If it initialized with an instance of Flask application then webassets
    Jinja2 extension is automatically registered.
    """

    config_storage_class = FlaskConfigStorage
    resolver_class = FlaskResolver

    def __init__(self, app=None):
        self.app = app
        super(Environment, self).__init__()
        if app:
            self.init_app(app)

    @property
    def _app(self):
        """The application object to work with; this is either the app
        that we have been bound to, or the current application.
        """
        if self.app is not None:
            return self.app

        if has_request_context():
            return request_ctx.app

        if has_app_context():
            return app_ctx.app

        raise RuntimeError('assets instance not bound to an application, '+
                            'and no application in current context')



    # XXX: This is required because in a couple of places, webassets 0.6
    # still access env.directory, at one point even directly. We need to
    # fix this for 0.6 compatibility, but it might be preferable to
    # introduce another API similar to _normalize_source_path() for things
    # like the cache directory and output files.
    def set_directory(self, directory):
        self.config['directory'] = directory
    def get_directory(self):
        if self.config.get('directory') is not None:
            return self.config['directory']
        return get_static_folder(self._app)
    directory = property(get_directory, set_directory, doc=
    """The base directory to which all paths will be relative to.
    """)
    def set_url(self, url):
        self.config['url'] = url
    def get_url(self):
        if self.config.get('url') is not None:
            return self.config['url']
        return self._app.static_url_path
    url = property(get_url, set_url, doc=
    """The base url to which all static urls will be relative to.""")

    def init_app(self, app):
        app.jinja_env.add_extension('webassets.ext.jinja2.AssetsExtension')
        app.jinja_env.assets_environment = self

    def from_yaml(self, path):
        """Register bundles from a YAML configuration file"""
        bundles = YAMLLoader(path).load_bundles()
        for name in bundles:
            self.register(name, bundles[name])

    def from_module(self, path):
        """Register bundles from a Python module"""
        bundles = PythonLoader(path).load_bundles()
        for name in bundles:
            self.register(name, bundles[name])


try:
    import click
    from flask import cli
except ImportError:
    pass
else:
    def _webassets_cmd(cmd):
        """Helper to run a webassets command."""
        from webassets.script import CommandLineEnvironment
        logger = logging.getLogger('webassets')
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.DEBUG)
        cmdenv = CommandLineEnvironment(
            current_app.jinja_env.assets_environment, logger
        )
        getattr(cmdenv, cmd)()


    @click.group()
    def assets():
        """Web assets commands."""


    @assets.command()
    @cli.with_appcontext
    def build():
        """Build bundles."""
        _webassets_cmd('build')


    @assets.command()
    @cli.with_appcontext
    def clean():
        """Clean bundles."""
        _webassets_cmd('clean')


    @assets.command()
    @cli.with_appcontext
    def watch():
        """Watch bundles for file changes."""
        _webassets_cmd('watch')

    __all__ = __all__ + ('assets', 'build', 'clean', 'watch')
