"""``@api_view`` for ``async def`` function views.

Adapted from Django REST framework 3.16's ``api_view`` (BSD-3-Clause); see
docs/upstream.md. The DRF policy decorators (``permission_classes`` etc.) are
re-exported unchanged.
"""

from __future__ import annotations

import types
from typing import Any, Callable

from asgiref.sync import iscoroutinefunction
from rest_framework.decorators import (  # noqa: F401  (re-exported)
    action,
    authentication_classes,
    parser_classes,
    permission_classes,
    renderer_classes,
    schema,
    throttle_classes,
)

from .views import APIView


def api_view(http_method_names: list[str] | None = None) -> Callable[[Any], Any]:
    http_method_names = ["GET"] if http_method_names is None else http_method_names

    def decorator(func: Any) -> Any:
        assert not isinstance(http_method_names, types.FunctionType), (
            "@api_view missing list of allowed HTTP methods"
        )
        assert isinstance(http_method_names, (list, tuple)), (
            "@api_view expected a list of strings, received %s"
            % type(http_method_names).__name__
        )
        WrappedAPIView = type("WrappedAPIView", (APIView,), {"__doc__": func.__doc__})
        allowed_methods = set(http_method_names) | {"options"}
        WrappedAPIView.http_method_names = [method.lower() for method in allowed_methods]

        if iscoroutinefunction(func):
            async def handler(self: Any, *args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)
        else:
            def handler(self: Any, *args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)

        for method in http_method_names:
            setattr(WrappedAPIView, method.lower(), handler)
        WrappedAPIView.__name__ = func.__name__
        WrappedAPIView.__module__ = func.__module__
        for name in ("renderer_classes", "parser_classes", "authentication_classes",
                     "throttle_classes", "permission_classes", "schema"):
            setattr(WrappedAPIView, name, getattr(func, name, getattr(APIView, name)))
        return WrappedAPIView.as_view()

    return decorator
