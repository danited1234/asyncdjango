from __future__ import annotations

from rest_framework import exceptions
from rest_framework.request import Request

from ._compat import resolve


class AsyncRequest(Request):
    """DRF ``Request`` whose authenticators may be ``async def``.

    ``APIView`` authenticates during ``initial()`` on the native path, so
    ``request.user`` is already resolved when a handler reads it.
    """

    def _authenticate(self) -> None:
        for authenticator in self.authenticators:
            try:
                user_auth_tuple = resolve(authenticator.authenticate(self))
            except exceptions.APIException:
                self._not_authenticated()
                raise
            if user_auth_tuple is not None:
                self._authenticator = authenticator
                self.user, self.auth = user_auth_tuple
                return
        self._not_authenticated()
