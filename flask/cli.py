# -*- coding: utf-8 -*-
"""
    flask.run
    ~~~~~~~~~

    A simple command line application to run flask apps.

    :copyright: (c) 2014 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""

import os
import sys
from threading import Lock
from contextlib import contextmanager

import click

from ._compat import iteritems


class NoAppException(click.UsageError):
    """Raised if an application cannot be found or loaded."""


def find_best_app(module):
    """Given a module instance this tries to find the best possible
    application in the module or raises an exception.
    """
    from . import Flask

    # Search for the most common names first.
    for attr_name in 'app', 'application':
        app = getattr(module, attr_name, None)
        if app is not None and isinstance(app, Flask):
            return app

    # Otherwise find the only object that is a Flask instance.
    matches = [v for k, v in iteritems(module.__dict__)
               if isinstance(v, Flask)]

    if len(matches) == 1:
        return matches[0]
    raise NoAppException('Failed to find application in module "%s".  Are '
                         'you sure it contains a Flask application?  Maybe '
                         'you wrapped it in a WSGI middleware or you are '
                         'using a factory function.' % module.__name__)


def prepare_exec_for_file(filename):
    """Given a filename this will try to calculate the python path, add it
    to the search path and return the actual module name that is expected.
    """
    module = []

    # Chop off file extensions or package markers
    if filename.endswith('.py'):
        filename = filename[:-3]
    elif os.path.split(filename)[1] == '__init__.py':
        filename = os.path.dirname(filename)
    else:
        raise NoAppException('The file provided (%s) does exist but is not a '
                             'valid Python file.  This means that it cannot '
                             'be used as application.  Please change the '
                             'extension to .py' % filename)
    filename = os.path.realpath(filename)

    dirpath = filename
    while 1:
        dirpath, extra = os.path.split(dirpath)
        module.append(extra)
        if not os.path.isfile(os.path.join(dirpath, '__init__.py')):
            break

    sys.path.insert(0, dirpath)
    return '.'.join(module[::-1])


def locate_app(app_id):
    """Attempts to locate the application."""
    if ':' in app_id:
        module, app_obj = app_id.split(':', 1)
    else:
        module = app_id
        app_obj = None

    __import__(module)
    mod = sys.modules[module]
    if app_obj is None:
        app = find_best_app(mod)
    else:
        app = getattr(mod, app_obj, None)
        if app is None:
            raise RuntimeError('Failed to find application in module "%s"'
                               % module)

    return app


class DispatchingApp(object):
    """Special application that dispatches to a flask application which
    is imported by name on first request.  This is safer than importing
    the application upfront because it means that we can forward all
    errors for import problems into the browser as error.
    """

    def __init__(self, loader, use_eager_loading=False):
        self.loader = loader
        self._app = None
        self._lock = Lock()
        if use_eager_loading:
            self._load_unlocked()

    def _load_unlocked(self):
        self._app = rv = self.loader()
        return rv

    def __call__(self, environ, start_response):
        if self._app is not None:
            return self._app(environ, start_response)
        with self._lock:
            if self._app is not None:
                rv = self._app
            else:
                rv = self._load_unlocked()
            return rv(environ, start_response)


class ScriptInfo(object):
    """Help object to deal with Flask applications.  This is usually not
    necessary to interface with as it's used internally in the dispatching
    to click.
    """

    def __init__(self, app_import_path=None, debug=None, create_app=None):
        #: The application import path
        self.app_import_path = app_import_path
        #: The debug flag.  If this is not None, the application will
        #: automatically have it's debug flag overridden with this value.
        self.debug = debug
        #: Optionally a function that is passed the script info to create
        #: the instance of the application.
        self.create_app = create_app
        #: A dictionary with arbitrary data that can be associated with
        #: this script info.
        self.data = {}
        self._loaded_app = None

    def load_app(self):
        """Loads the Flask app (if not yet loaded) and returns it.  Calling
        this multiple times will just result in the already loaded app to
        be returned.
        """
        if self._loaded_app is not None:
            return self._loaded_app
        if self.create_app is not None:
            rv = self.create_app(self)
        else:
            if self.app_import_path is None:
                raise NoAppException('Could not locate Flask application. '
                                     'You did not provide FLASK_APP or the '
                                     '--app parameter.')
            rv = locate_app(self.app_import_path)
        if self.debug is not None:
            rv.debug = self.debug
        self._loaded_app = rv
        return rv

    @contextmanager
    def conditional_context(self, with_context=True):
        """Creates an application context or not, depending on the given
        parameter but always works as context manager.  This is just a
        shortcut for a common operation.
        """
        if with_context:
            with self.load_app().app_context() as ctx:
                yield ctx
        else:
            yield None


pass_script_info = click.make_pass_decorator(ScriptInfo)


def without_appcontext(f):
    """Marks a click callback so that it does not get a app context
    created.  This only works for commands directly registered to
    the toplevel system.  This really is only useful for very
    special commands like the runserver one.
    """
    f.__flask_without_appcontext__ = True
    return f


def set_debug_value(ctx, param, value):
    ctx.ensure_object(ScriptInfo).debug = value


def set_app_value(ctx, param, value):
    if value is not None:
        if os.path.isfile(value):
            value = prepare_exec_for_file(value)
        elif '.' not in sys.path:
            sys.path.insert(0, '.')
    ctx.ensure_object(ScriptInfo).app_import_path = value


debug_option = click.Option(['--debug/--no-debug'],
    help='Enable or disable debug mode.',
    default=None, callback=set_debug_value)


app_option = click.Option(['-a', '--app'],
    help='The application to run',
    callback=set_app_value, is_eager=True)


class FlaskGroup(click.Group):
    """Special subclass of the a regular click group that supports loading
    more commands from the configured Flask app.  Normally a developer
    does not have to interface with this class but there are some very
    advanced usecases for which it makes sense to create an instance of
    this.

    For information as of why this is useful see :ref:`custom-scripts`.

    :param add_default_commands: if this is True then the default run and
                                 shell commands wil be added.
    :param add_app_option: adds the default ``--app`` option.  This gets
                           automatically disabled if a `create_app`
                           callback is defined.
    :param add_debug_option: adds the default ``--debug`` option.
    :param create_app: an optional callback that is passed the script info
                       and returns the loaded app.
    """

    def __init__(self, add_default_commands=True, add_app_option=None,
                 add_debug_option=True, create_app=None, **extra):
        params = list(extra.pop('params', None) or ())
        if add_app_option is None:
            add_app_option = create_app is None
        if add_app_option:
            params.append(app_option)
        if add_debug_option:
            params.append(debug_option)

        click.Group.__init__(self, params=params, **extra)
        self.create_app = create_app

        if add_default_commands:
            self.add_command(run_command)
            self.add_command(shell_command)

    def get_command(self, ctx, name):
        # We load built-in commands first as these should always be the
        # same no matter what the app does.  If the app does want to
        # override this it needs to make a custom instance of this group
        # and not attach the default commands.
        #
        # This also means that the script stays functional in case the
        # application completely fails.
        rv = click.Group.get_command(self, ctx, name)
        if rv is not None:
            return rv

        info = ctx.ensure_object(ScriptInfo)
        try:
            rv = info.load_app().cli.get_command(ctx, name)
            if rv is not None:
                return rv
        except NoAppException:
            pass

    def list_commands(self, ctx):
        # The commands available is the list of both the application (if
        # available) plus the builtin commands.
        rv = set(click.Group.list_commands(self, ctx))
        info = ctx.ensure_object(ScriptInfo)
        try:
            rv.update(info.load_app().cli.list_commands(ctx))
        except Exception:
            # Here we intentionally swallow all exceptions as we don't
            # want the help page to break if the app does not exist.
            # If someone attempts to use the command we try to create
            # the app again and this will give us the error.
            pass
        return sorted(rv)

    def invoke_subcommand(self, ctx, cmd, cmd_name, args):
        with_context = cmd.callback is None or \
           not getattr(cmd.callback, '__flask_without_appcontext__', False)

        with ctx.find_object(ScriptInfo).conditional_context(with_context):
            return click.Group.invoke_subcommand(
                self, ctx, cmd, cmd_name, args)

    def main(self, *args, **kwargs):
        obj = kwargs.get('obj')
        if obj is None:
            obj = ScriptInfo(create_app=self.create_app)
        kwargs['obj'] = obj
        kwargs.setdefault('auto_envvar_prefix', 'FLASK')
        return click.Group.main(self, *args, **kwargs)


def script_info_option(*args, **kwargs):
    """This decorator works exactly like :func:`click.option` but is eager
    by default and stores the value in the :attr:`ScriptInfo.data`.  This
    is useful to further customize an application factory in very complex
    situations.

    :param script_info_key: this is a mandatory keyword argument which
                            defines under which data key the value should
                            be stored.
    """
    try:
        key = kwargs.pop('script_info_key')
    except LookupError:
        raise TypeError('script_info_key not provided.')

    real_callback = kwargs.get('callback')
    def callback(ctx, value):
        if real_callback is not None:
            value = real_callback(ctx, value)
        ctx.ensure_object(ScriptInfo).data[key] = value
        return value

    kwargs['callback'] = callback
    kwargs.setdefault('is_eager', True)
    return click.option(*args, **kwargs)


@click.command('run', short_help='Runs a development server.')
@click.option('--host', '-h', default='127.0.0.1',
              help='The interface to bind to.')
@click.option('--port', '-p', default=5000,
              help='The port to bind to.')
@click.option('--reload/--no-reload', default=None,
              help='Enable or disable the reloader.  By default the reloader '
              'is active if debug is enabled.')
@click.option('--debugger/--no-debugger', default=None,
              help='Enable or disable the debugger.  By default the debugger '
              'is active if debug is enabled.')
@click.option('--eager-loading/--lazy-loader', default=None,
              help='Enable or disable eager loading.  By default eager '
              'loading is enabled if the reloader is disabled.')
@click.option('--with-threads/--without-threads', default=False,
              help='Enable or disable multithreading.')
@without_appcontext
@pass_script_info
def run_command(info, host, port, reload, debugger, eager_loading,
                with_threads):
    """Runs a local development server for the Flask application.

    This local server is recommended for development purposes only but it
    can also be used for simple intranet deployments.  By default it will
    not support any sort of concurrency at all to simplify debugging.  This
    can be changed with the --with-threads option which will enable basic
    multithreading.

    The reloader and debugger are by default enabled if the debug flag of
    Flask is enabled and disabled otherwise.
    """
    from werkzeug.serving import run_simple
    if reload is None:
        reload = info.debug
    if debugger is None:
        debugger = info.debug
    if eager_loading is None:
        eager_loading = not reload

    app = DispatchingApp(info.load_app, use_eager_loading=eager_loading)

    # Extra startup messages.  This depends a but on Werkzeug internals to
    # not double execute when the reloader kicks in.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        # If we have an import path we can print it out now which can help
        # people understand what's being served.  If we do not have an
        # import path because the app was loaded through a callback then
        # we won't print anything.
        if info.app_import_path is not None:
            print(' * Serving Flask app "%s"' % info.app_import_path)
        if info.debug is not None:
            print(' * Forcing debug %s' % (info.debug and 'on' or 'off'))

    run_simple(host, port, app, use_reloader=reload,
               use_debugger=debugger, threaded=with_threads)


@click.command('shell', short_help='Runs a shell in the app context.')
def shell_command():
    """Runs an interactive Python shell in the context of a given
    Flask application.  The application will populate the default
    namespace of this shell according to it's configuration.

    This is useful for executing small snippets of management code
    without having to manually configuring the application.
    """
    import code
    from flask.globals import _app_ctx_stack
    app = _app_ctx_stack.top.app
    banner = 'Python %s on %s\nApp: %s%s\nInstance: %s' % (
        sys.version,
        sys.platform,
        app.import_name,
        app.debug and ' [debug]' or '',
        app.instance_path,
    )
    code.interact(banner=banner, local=app.make_shell_context())


cli = FlaskGroup(help="""\
This shell command acts as general utility script for Flask applications.

It loads the application configured (either through the FLASK_APP environment
variable or the --app parameter) and then provides commands either provided
by the application or Flask itself.

The most useful commands are the "run" and "shell" command.

Example usage:

  flask --app=hello --debug run
""")


def main(as_module=False):
    this_module = __package__ + '.cli'
    args = sys.argv[1:]

    if as_module:
        if sys.version_info >= (2, 7):
            name = 'python -m ' + this_module.rsplit('.', 1)[0]
        else:
            name = 'python -m ' + this_module

        # This module is always executed as "python -m flask.run" and as such
        # we need to ensure that we restore the actual command line so that
        # the reloader can properly operate.
        sys.argv = ['-m', this_module] + sys.argv[1:]
    else:
        name = None

    cli.main(args=args, prog_name=name)


if __name__ == '__main__':
    main(as_module=True)
