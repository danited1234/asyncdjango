"""Pluggable, awaited request authentication without worker-thread bridges."""

from __future__ import annotations

from typing import Any

from .asgi import Request


class AuthenticationFailed(Exception):
    pass


class BaseAuthentication:
    async def authenticate(self, request: Request) -> tuple[Any, Any] | None:
        raise NotImplementedError


class TokenAuthentication(BaseAuthentication):
    """DRF-style Token header; subclass authenticate_credentials for lookup."""

    keyword = b"Token"

    async def authenticate_credentials(self, key: str) -> tuple[Any, Any]:
        raise NotImplementedError("Provide a native async token lookup")

    async def authenticate(self, request: Request) -> tuple[Any, Any] | None:
        headers = [value for name, value in request.scope.get("headers", [])
                   if name.lower() == b"authorization"]
        if not headers:
            return None
        if len(headers) != 1:
            raise AuthenticationFailed("Invalid authorization header")
        parts = headers[0].split()
        if not parts or parts[0].lower() != self.keyword.lower():
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Invalid token header")
        try:
            key = parts[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed("Invalid token header") from exc
        if not key:
            raise AuthenticationFailed("Invalid token header")
        return await self.authenticate_credentials(key)
