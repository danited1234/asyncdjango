from __future__ import annotations

from typing import Any

from asgiref.sync import iscoroutinefunction
from rest_framework.views import APIView as DRFAPIView

from ..db.scope import run_native
from ._compat import resolve
from .requests import AsyncRequest


class APIView(DRFAPIView):
    """DRF ``APIView`` that dispatches ``async def`` handlers natively.

    ``initial()`` (content negotiation, versioning, authentication,
    permissions, throttles) and ``handle_exception()`` run in one native
    greenlet per step: synchronous hooks run on the event loop with database
    I/O awaited, and hooks written as ``async def`` are awaited. Views whose
    handlers are all synchronous keep DRF's synchronous dispatch.
    """

    def initialize_request(self, request: Any, *args: Any, **kwargs: Any) -> AsyncRequest:
        parser_context = self.get_parser_context(request)
        return AsyncRequest(
            request,
            parsers=self.get_parsers(),
            authenticators=self.get_authenticators(),
            negotiator=self.get_content_negotiator(),
            parser_context=parser_context,
        )

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(self, "view_is_async", False):
            return self.async_dispatch(request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    async def async_dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self.args = args
        self.kwargs = kwargs
        request = self.initialize_request(request, *args, **kwargs)
        self.request = request
        self.headers = self.default_response_headers
        try:
            await run_native(self.initial, request, *args, **kwargs)
            if request.method.lower() in self.http_method_names:
                handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
            else:
                handler = self.http_method_not_allowed
            if iscoroutinefunction(handler):
                response = await handler(request, *args, **kwargs)
            else:
                response = await run_native(handler, request, *args, **kwargs)
        except Exception as exc:
            response = await run_native(self.handle_exception, exc)
        self.response = self.finalize_response(request, response, *args, **kwargs)
        return self.response

    def check_permissions(self, request: Any) -> None:
        for permission in self.get_permissions():
            if not resolve(permission.has_permission(request, self)):
                self.permission_denied(
                    request,
                    message=getattr(permission, "message", None),
                    code=getattr(permission, "code", None),
                )

    def check_object_permissions(self, request: Any, obj: Any) -> None:
        for permission in self.get_permissions():
            if not resolve(permission.has_object_permission(request, self, obj)):
                self.permission_denied(
                    request,
                    message=getattr(permission, "message", None),
                    code=getattr(permission, "code", None),
                )

    def check_throttles(self, request: Any) -> None:
        throttle_durations = []
        for throttle in self.get_throttles():
            if not resolve(throttle.allow_request(request, self)):
                throttle_durations.append(resolve(throttle.wait()))
        if throttle_durations:
            durations = [duration for duration in throttle_durations if duration is not None]
            self.throttled(request, max(durations, default=None))

    async def acheck_permissions(self, request: Any) -> None:
        await run_native(self.check_permissions, request)

    async def acheck_object_permissions(self, request: Any, obj: Any) -> None:
        await run_native(self.check_object_permissions, request, obj)

    async def acheck_throttles(self, request: Any) -> None:
        await run_native(self.check_throttles, request)
