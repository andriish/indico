# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import time
from datetime import timedelta

import limits
from flask import has_request_context, request
from flask_limiter import Limiter
from limits import parse_many

from indico.util.signals import make_interceptable


class IndicoRedisStorage(limits.storage.RedisStorage):
    # This is needed so redis also works with unix sockets. For details, see:
    # - https://github.com/indico/indico/issues/5391
    # - https://github.com/alisaifee/limits/issues/128
    STORAGE_SCHEME = ['redis', 'rediss', 'unix']


class RateLimit:
    def __init__(self, limiter, key_func, scope, limits):
        self.limiter = limiter
        self.key_func = key_func
        self.scope = scope
        self.limits = limits

    def _get_args(self, args):
        # scope args go directly the redis keys and are separated by slashes,
        # so we want to avoid cases where there's some leakage between different
        # rate limiters if someone ends up passing user-provided args (even though
        # this would be a bad idea in general)
        args = [x.replace('/', '-') if isinstance(x, str) else x for x in args]
        if not self.key_func:
            return self.scope, *args
        return self.key_func(), self.scope, *args

    def hit(self, *args):
        """Check the rate limit and increment the counter.

        This method should be used when performing an action that should
        count against the user's rate limit.

        :param args: Additional args to include in the scope, such as a user id.
        """
        if self.limits is None:
            return True
        args = self._get_args(args)
        return any(self.limiter.limiter.hit(lim, *args) for lim in self.limits)

    def test(self, *args):
        """Check the rate limit without incrementing the counter.

        This method should be used when you just want to see if the rate limit
        has been triggered (e.g. to show a message when loading a form), without
        counting the action against the rate limit.

        :param args: Additional args to include in the scope, such as a user id.
        """
        if self.limits is None:
            return True
        args = self._get_args(args)
        return any(self.limiter.limiter.test(lim, *args) for lim in self.limits)

    def clear(self, *args):
        """Reset the rate limit.

        This method should be used when you want to reset any previous hits that
        may still be affecting the limit.

        :param args: Additional args to include in the scope, such as a user id.
        """
        if self.limits is None:
            return
        args = self._get_args(args)
        for lim in self.limits:
            self.limiter.limiter.clear(lim, *args)

    def get_reset_delay(self, *args):
        """Get the duration until the rate limit resets.

        :param args: Additional args to include in the scope, such as a user id.
        """
        if self.limits is None:
            return timedelta()
        args = self._get_args(args)
        reset = min(self.limiter.limiter.get_window_stats(lim, *args)[0] for lim in self.limits)
        return timedelta(seconds=reset - int(time.time()))

    def __repr__(self):
        limits = '; '.join(str(lim) for lim in self.limits) if self.limits is not None else 'unlimited'
        return f'<RateLimit({self.scope}): {limits}>'


def make_rate_limiter(scope, limits, *, by_ip=True):
    """Create a rate limiter.

    Multiple limits can be separated with a semicolon; in that case
    all limits are checked until one succeeds. This allows specifying
    a somewhat strict limit, but then a higher limit over a longer period
    of time to allow for bursts.
    """
    limits = list(parse_many(limits)) if limits is not None else None
    return RateLimit(limiter, limiter._key_func if by_ip else None, scope, limits)


@make_interceptable
def _limiter_key():
    return request.remote_addr if has_request_context() else 'dummy.ip'


limiter = Limiter(key_func=_limiter_key, strategy='moving-window', auto_check=False)
