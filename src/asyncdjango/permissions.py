"""Async-compatible DRF-style view and object permissions."""

from __future__ import annotations

from typing import Any

from .asgi import Request

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class BasePermission:
    async def has_permission(self, request: Request, view: Any) -> bool:
        return True

    async def has_object_permission(self, request: Request, view: Any,
                                    obj: Any) -> bool:
        return True


class AllowAny(BasePermission):
    pass


class IsAuthenticated(BasePermission):
    async def has_permission(self, request: Request, view: Any) -> bool:
        return bool(getattr(request.user, "is_authenticated", False))


class IsAuthenticatedOrReadOnly(BasePermission):
    async def has_permission(self, request: Request, view: Any) -> bool:
        return request.method in SAFE_METHODS or bool(
            getattr(request.user, "is_authenticated", False)
        )
