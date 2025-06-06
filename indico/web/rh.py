# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import cProfile
import inspect
import itertools
import os
import time
from functools import partial, wraps

import jsonschema
import sentry_sdk
from flask import current_app, g, redirect, request, session
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.exceptions import BadRequest, Forbidden, MethodNotAllowed, NotFound
from werkzeug.routing import BuildError
from werkzeug.wrappers import Response

from indico.core import signals
from indico.core.config import config
from indico.core.db import db
from indico.core.db.sqlalchemy.core import handle_sqlalchemy_database_error
from indico.core.logger import Logger
from indico.core.notifications import flush_email_queue, init_email_queue
from indico.util.i18n import _
from indico.util.locators import get_locator
from indico.util.signals import values_from_signal
from indico.web.flask.util import url_for
from indico.web.util import get_request_user


HTTP_VERBS = {'GET', 'PATCH', 'POST', 'PUT', 'DELETE'}
logger = Logger.get('rh')


class RH:
    CSRF_ENABLED = True  # require a csrf_token when accessing the RH with anything but GET
    EVENT_FEATURE = None  # require a certain event feature when accessing the RH. See `EventFeature` for details
    DENY_FRAMES = False  # whether to send an X-Frame-Options:DENY header

    #: A dict specifying how the url should be normalized.
    #: `args` is a dictionary mapping view args keys to callables
    #: used to retrieve the expected value for those arguments if they
    #: are present in the request's view args.
    #: `locators` is a set of callables returning objects with locators.
    #: `preserved_args` is a set of view arg names which will always
    #: be copied from the current request if present.
    #: `skipped_args` is a set of view arg names which will be ignored if
    #: present in the request or the locators.
    #: `copy_query_args` specified arguments that are in the query string
    #: but may be provided by one of the locators and thus need to be taken
    #: from the query string when checking if a redirect is needed.
    #: The callables are always invoked with a single `self` argument
    #: containing the RH instance.
    #: `endpoint` may be used to specify the endpoint used to build
    #: the URL in case of a redirect.  Usually this should not be used
    #: in favor of ``request.endpoint`` being used if no custom endpoint
    #: is set.
    #: Arguments specified in the `defaults` of any rule matching the
    #: current endpoint are always excluded when checking if the args
    #: match or when building a new URL.
    #: If the view args built from the returned objects do not match
    #: the request's view args, a redirect is issued automatically.
    #: If the request is not using GET/HEAD, a 404 error is raised
    #: instead of a redirect since such requests cannot be redirected
    #: but executing them on the wrong URL may pose a security risk in
    #: case and of the non-relevant URL segments is used for access
    #: checks.
    normalize_url_spec = {
        'args': {},
        'locators': set(),
        'preserved_args': set(),
        'skipped_args': set(),
        'copy_query_args': set(),
        'endpoint': None
    }

    #: Like `normalize_url_spec`, but runs after the access check succeeded
    normalize_url_spec_late = None

    def __init__(self):
        self.commit = True
        self.noindex = False

    # Methods =============================================================

    def validate_json(self, schema, json=None):
        """Validate the request's JSON payload using a JSON schema.

        :param schema: The JSON schema used for validation.
        :param json: The JSON object (defaults to ``request.json``)
        :raises BadRequest: if the JSON validation failed
        """
        if json is None:
            json = request.json
        try:
            jsonschema.validate(json, schema)
        except jsonschema.ValidationError as exc:
            raise BadRequest(f'Invalid JSON payload: {exc}')

    @property
    def csrf_token(self):
        return session.csrf_token if session.csrf_protected else ''

    def normalize_url(self, *, late=False):
        """Perform URL normalization.

        This uses the :attr:`normalize_url_spec` to check if the URL
        params are what they should be and redirects or fails depending
        on the HTTP method used if it's not the case.

        :return: ``None`` or a redirect response
        """
        spec = self.normalize_url_spec_late if late else self.normalize_url_spec
        base_spec = RH.normalize_url_spec_late if late else RH.normalize_url_spec

        if current_app.debug and spec is base_spec and spec is not None:
            # in case of ``class SomeRH(RH, MixinWithNormalization)``
            # the default value from `RH` overwrites the normalization
            # rule from ``MixinWithNormalization``.  this is never what
            # the developer wants so we fail if it happens.  the proper
            # solution is ``class SomeRH(MixinWithNormalization, RH)``
            cls = next((x
                        for x in inspect.getmro(self.__class__)
                        if (x is not RH and x is not self.__class__ and hasattr(x, 'normalize_url_spec') and
                            getattr(x, 'normalize_url_spec', None) is not base_spec)),
                       None)
            if cls is not None:
                raise Exception(f'Normalization rule of {cls} in {type(self)} is overwritten by base RH. '
                                'Put mixins with class-level attributes on the left of the base class')
        if not spec or not any(spec.values()):
            return
        spec = {
            'args': spec.get('args', {}),
            'locators': spec.get('locators', set()),
            'preserved_args': spec.get('preserved_args', set()),
            'skipped_args': spec.get('skipped_args', set()),
            'copy_query_args': spec.get('copy_query_args', set()),
            'endpoint': spec.get('endpoint'),
        }
        # Initialize the new view args with preserved arguments (since those would be lost otherwise)
        new_view_args = {k: v for k, v in request.view_args.items() if k in spec['preserved_args']}
        # Retrieve the expected values for all simple arguments (if they are currently present)
        for key, getter in spec['args'].items():
            if key in request.view_args:
                new_view_args[key] = getter(self)
        # Retrieve the expected values from locators
        prev_locator_args = {}
        for getter in spec['locators']:
            value = getter(self)
            if value is None:
                raise NotFound('The URL contains invalid data. Please go to the previous page and refresh it.')
            locator_args = {k: v for k, v in get_locator(value).items() if k not in spec['skipped_args']}
            reused_keys = set(locator_args) & prev_locator_args.keys()
            if any(locator_args[k] != prev_locator_args[k] for k in reused_keys):
                raise NotFound('The URL contains invalid data. Please go to the previous page and refresh it.')
            new_view_args.update(locator_args)
            prev_locator_args.update(locator_args)
        # Get all default values provided by the url map for the endpoint
        defaults = set(itertools.chain.from_iterable(r.defaults
                                                     for r in current_app.url_map.iter_rules(request.endpoint)
                                                     if r.defaults))

        def _convert(v):
            # some legacy code has numeric ids in the locator data, but still takes
            # string ids in the url rule (usually for `confId` which is now numeric and
            # called `event_id`, but just in case there's any other code that also has
            # this ugly string/int mix we'll leave this here - there is no harm in
            # passing strings to `url_for` even for int segments)
            return str(v) if isinstance(v, int) else v

        provided = {k: _convert(v) for k, v in request.view_args.items() if k not in defaults}
        if spec['copy_query_args']:
            provided.update((k, _convert(v)) for k, v in request.args.items() if k in spec['copy_query_args'])
        new_view_args = {k: _convert(v) for k, v in new_view_args.items() if v is not None}
        if new_view_args != provided:
            if request.method in {'GET', 'HEAD'}:
                endpoint = spec['endpoint'] or request.endpoint
                build_args = request.args.to_dict() | new_view_args
                try:
                    return redirect(url_for(endpoint, **build_args))
                except BuildError as e:
                    if current_app.debug:
                        raise
                    logger.warning('BuildError during normalization: %s', e)
                    raise NotFound
            else:
                raise NotFound('The URL contains invalid data. Please go to the previous page and refresh it.')

    def _process_args(self):
        """
        This method is called before _check_access and url normalization
        and is a good place to fetch objects from the database based on
        variables from request params.
        """

    def _check_access(self):
        """
        This method is called after _process_args and is a good place
        to check if the user is permitted to perform some actions.
        """

    def _process(self):
        """Dispatch to a method named ``_process_<verb>``.

        Except for RESTful endpoints it is usually best to just
        override this method, especially when using WTForms.
        """
        method = getattr(self, '_process_' + request.method, None)
        if method is None:
            valid_methods = [m for m in HTTP_VERBS if hasattr(self, '_process_' + m)]
            raise MethodNotAllowed(valid_methods)
        return method()

    def _check_csrf(self):
        if get_request_user()[1] in ('oauth', 'signed_url'):
            # no csrf checks needed since both of these auth methods require secrets
            # not available to a malicious site (and if they were, they wouldn't have
            # to use CSRF to abuse them)
            return
        token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        if token is None:
            # Might be a WTForm with a prefix. In that case the field name is '<prefix>-csrf_token'
            token = next((v for k, v in request.form.items() if k.endswith('-csrf_token')), None)
        if self.CSRF_ENABLED and request.method != 'GET' and token != session.csrf_token:
            msg = _("It looks like there was a problem with your current session. Please use your browser's back "
                    'button, reload the page and try again.')
            raise BadRequest(msg)

    def _check_terms(self):
        from indico.modules.legal import confirm_rh_terms_required
        return confirm_rh_terms_required()

    def _check_event_feature(self):
        from indico.modules.events.features.util import require_feature
        event_id = request.view_args.get('event_id')
        if event_id is not None:
            require_feature(event_id, self.EVENT_FEATURE)

    def _do_process(self):
        try:
            args_result = self._process_args()
            signals.rh.process_args.send(type(self), rh=self, result=args_result)
            if isinstance(args_result, (current_app.response_class, Response)):
                return args_result
        except NoResultFound:  # sqlalchemy .one() not finding anything
            raise NotFound(_('The specified item could not be found.'))

        if rv := self.normalize_url():
            return rv

        signal_rv = values_from_signal(signals.rh.before_check_access.send(type(self), rh=self))
        if not all(signal_rv):
            raise Forbidden('Unauthorized access.')
        if not signal_rv:
            self._check_access()
        signals.rh.check_access.send(type(self), rh=self)

        if rv := self.normalize_url(late=True):
            return rv

        signal_rv = values_from_signal(signals.rh.before_process.send(type(self), rh=self),
                                       single_value=True, as_list=True)
        if signal_rv and len(signal_rv) != 1:
            raise Exception('More than one signal handler returned custom RH result')
        elif signal_rv:
            return signal_rv[0]

        if config.PROFILE:
            result = [None]
            profile_path = os.path.join(config.TEMP_DIR, f'{type(self).__name__}-{time.time()}.prof')
            cProfile.runctx('result[0] = self._process()', globals(), locals(), profile_path)
            rv = result[0]
        else:
            rv = self._process()

        signal_rv = values_from_signal(signals.rh.process.send(type(self), rh=self, result=rv),
                                       single_value=True, as_list=True)
        if signal_rv and len(signal_rv) != 1:
            raise Exception('More than one signal handler returned new RH result')
        elif signal_rv:
            return signal_rv[0]
        else:
            return rv

    def process(self):
        if request.method not in HTTP_VERBS:
            # Just to be sure that we don't get some crappy http verb we don't expect
            raise BadRequest

        res = ''
        g.rh = self
        sentry_sdk.set_tag('rh', type(self).__name__)

        if self.EVENT_FEATURE is not None:
            self._check_event_feature()

        logger.info('%s %s [IP=%s] [PID=%s]',
                    request.method, request.relative_url, request.remote_addr, os.getpid())

        try:
            init_email_queue()
            self._check_csrf()
            if terms_response := self._check_terms():
                return terms_response

            res = self._do_process()
            signals.core.after_process.send()

            if self.commit:
                db.session.commit()
                flush_email_queue()
            else:
                db.session.rollback()
        except DatabaseError:
            db.session.rollback()
            handle_sqlalchemy_database_error()  # this will re-raise an exception
        except Exception:
            # rollback to avoid errors as rendering the error page
            # within the indico layout may trigger an auto-flush
            db.session.rollback()
            raise
        logger.debug('Request successful')

        if res is None:
            # flask doesn't accept None but we might be returning it in some places...
            res = ''

        response = current_app.make_response(res)
        if self.noindex:
            response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
        if self.DENY_FRAMES:
            response.headers['X-Frame-Options'] = 'DENY'
        return response


class RHSimple(RH):
    """A simple RH that calls a function to build the response.

    The preferred way to use this class is by using the
    `RHSimple.wrap_function` decorator.

    :param func: A function returning HTML
    """

    def __init__(self, func):
        RH.__init__(self)
        self.func = func

    def _process(self):
        return self.func()

    @classmethod
    def wrap_function(cls, func, *, disable_csrf_check=False):
        """Decorate a function to run within the RH's framework."""
        @wraps(func)
        def wrapper(*args, **kwargs):
            rh = cls(partial(func, *args, **kwargs))
            if disable_csrf_check:
                rh.CSRF_ENABLED = False
            return rh.process()

        return wrapper


class RHProtected(RH):
    """Base RH that requires the user to be authenticated."""

    def _require_user(self):
        if session.user is None:
            raise Forbidden

    def _check_access(self):
        self._require_user()


class RequireUserMixin:
    def _check_access(self):
        if session.user is None:
            raise Forbidden


def oauth_scope(scope):
    """Specify a custom OAuth scope needed to access an RH.

    By default RHs require one of the ``everything`` OAuth scopes to use
    them when authenticating with an OAuth token. These scopes are meant
    as wildcards for things that aren't official APIs though. Any RH that
    expose actual APIs should use scopes related to what they do (within
    reason of course).
    """
    # TODO: we should probably allow setting scopes for specific methods using
    # a syntax like `@oauth_scope('foo', 'read:foo')` which would require `foo`
    # or `read:foo` for GET requests and require `foo` for any other method
    def decorator(rh):
        rh._OAUTH_SCOPE = scope
        return rh
    return decorator


def custom_auth(rh):
    """Use custom (bearer token) auth for this RH.

    This is useful when creating endpoints that will handle Bearer tokens
    on their own and should never try to verify a token against Indico's
    own OAuth or personal tokens.

    It completely disables any of the core auth features, so neither bearer
    tokens nor session cookies nor signed URLs will be handled; this means
    the "current user" is always going to be None.
    """
    rh._DISABLE_CORE_AUTH = True
    return rh


def allow_signed_url(rh):
    """Allow accessing this RH using persistent signed URLs.

    By default RHs do not allow access using a persistent URL containing
    a ``user_token``. By decorating a RH with this decorator, the RH will
    allow requests authenticated using such a token.
    """
    rh._ALLOW_SIGNED_URL = True
    return rh


def json_errors(rh):
    """Specify that the RH should always jsonify errors.

    This is meant for API endpoints and similar places where errors
    should be returned as JSON and not as a formatted HTML error page.
    """
    rh._JSON_ERRORS = True
    return rh


def cors(rh=None, /, **options):
    """Enable CORS for the decorated RH."""

    def decorator(rh):
        rh._CORS = options
        return rh

    if rh is None:
        # if we used `@cors()` we need to return the inner decorator
        return decorator

    # in case of `@cors` we have the RH and can apply the actual decorator
    return decorator(rh)
