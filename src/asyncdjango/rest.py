"""Initial async APIView and model viewset conventions."""

from __future__ import annotations

import json
from typing import Any

from .asgi import ASGIApplication, JSONResponse, Request
from .authentication import AuthenticationFailed, BaseAuthentication
from .db.models import IntegerField, Model, QuerySet
from .filters import BaseFilterBackend, FilterError
from .pagination import PageNumberPagination, PaginationError
from .permissions import AllowAny, BasePermission
from .serializers import ModelSerializer, ValidationError
from .throttling import BaseThrottle


class PermissionDenied(Exception):
    pass


class Response(JSONResponse):
    """DRF-style JSON response spelling for the current renderer subset."""

    def __init__(self, data: Any = None, status: int | None = None,
                 headers: dict[str, str] | None = None) -> None:
        encoded_headers = [
            (name.lower().encode("ascii"), value.encode("utf-8"))
            for name, value in (headers or {}).items()
        ]
        super().__init__(data, status_code=200 if status is None else status,
                         headers=encoded_headers)


class APIView:
    authentication_classes: tuple[type[BaseAuthentication], ...] = ()
    permission_classes: tuple[type[BasePermission], ...] = (AllowAny,)
    throttle_classes: tuple[type[BaseThrottle], ...] = ()

    def get_authenticators(self) -> list[BaseAuthentication]:
        return [cls() for cls in self.authentication_classes]

    def get_permissions(self) -> list[BasePermission]:
        return [cls() for cls in self.permission_classes]

    def get_throttles(self) -> list[BaseThrottle]:
        return [cls() for cls in self.throttle_classes]

    async def check_object_permissions(self, request: Request, obj: Any) -> None:
        for permission in self.get_permissions():
            if not await permission.has_object_permission(request, self, obj):
                raise PermissionDenied

    async def _initial(self, request: Request) -> JSONResponse | None:
        authenticators = self.get_authenticators()
        auth_headers = []
        if authenticators and hasattr(authenticators[0], "keyword"):
            auth_headers = [(b"www-authenticate", authenticators[0].keyword)]
        try:
            for authenticator in authenticators:
                result = await authenticator.authenticate(request)
                if result is not None:
                    request.user, request.auth = result
                    break
        except AuthenticationFailed as exc:
            return JSONResponse({"detail": str(exc)}, 401, auth_headers)
        for permission in self.get_permissions():
            if not await permission.has_permission(request, self):
                status = 401 if self.authentication_classes and not getattr(
                    request.user, "is_authenticated", False
                ) else 403
                return JSONResponse({"detail": "Permission denied"}, status,
                                    auth_headers if status == 401 else None)
        for throttle in self.get_throttles():
            if not await throttle.allow_request(request, self):
                wait = throttle.retry_after() if hasattr(throttle, "retry_after") else None
                headers = [(b"retry-after", str(wait).encode())] if wait is not None else []
                return JSONResponse({"detail": "Request was throttled"}, 429, headers)
        return None

    async def dispatch_action(self, request: Request, action: str) -> JSONResponse:
        rejected = await self._initial(request)
        if rejected is not None:
            return rejected
        method = getattr(self, action, None)
        if method is None:
            return JSONResponse({"detail": "Method not allowed"}, 405)
        try:
            return await method(request)
        except PermissionDenied:
            return JSONResponse({"detail": "Permission denied"}, 403)
        except (FilterError, PaginationError) as exc:
            return JSONResponse({"detail": str(exc)}, 400)

    async def dispatch(self, request: Request) -> JSONResponse:
        return await self.dispatch_action(request, request.method.lower())

    @classmethod
    def as_view(cls):
        async def view(request: Request) -> JSONResponse:
            return await cls().dispatch(request)
        view.__asyncdjango_view_class__ = cls
        return view


class ModelViewSet(APIView):
    queryset: QuerySet
    serializer_class: type[ModelSerializer]
    filter_backends: tuple[type[BaseFilterBackend], ...] = ()
    pagination_class: type[PageNumberPagination] | None = None

    def get_queryset(self) -> QuerySet:
        return self.queryset.all()

    def filter_queryset(self, request: Request, queryset: QuerySet) -> QuerySet:
        for backend_class in self.filter_backends:
            queryset = backend_class().filter_queryset(request, queryset, self)
        return queryset

    def _pk(self, request: Request) -> Any:
        raw = request.path_params["pk"]
        model: type[Model] = self.get_queryset().model
        field = model._meta.fields[model._meta.pk_name]
        if isinstance(field, IntegerField):
            try:
                return int(raw)
            except ValueError:
                return raw
        return raw

    async def list(self, request: Request) -> JSONResponse:
        query = self.filter_queryset(request, self.get_queryset())
        if self.pagination_class is not None:
            paginator = self.pagination_class()
            objects = await paginator.paginate_queryset(query, request)
            return paginator.get_paginated_response(
                self.serializer_class(objects, many=True).data
            )
        objects = await query.fetch_all()
        return JSONResponse(self.serializer_class(objects, many=True).data)

    async def retrieve(self, request: Request) -> JSONResponse:
        query = self.filter_queryset(request, self.get_queryset())
        try:
            instance = await query.get(pk=self._pk(request))
        except query.model.DoesNotExist:
            return JSONResponse({"detail": "Not found"}, 404)
        await self.check_object_permissions(request, instance)
        return JSONResponse(self.serializer_class(instance).data)

    async def create(self, request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"detail": "Invalid JSON"}, 400)
        serializer = self.serializer_class(data=payload)
        if not await serializer.ais_valid():
            return JSONResponse(serializer.errors, 400)
        try:
            await serializer.asave()
        except ValidationError as exc:
            return JSONResponse(exc.errors, 400)
        return JSONResponse(serializer.data, 201)

    async def update(self, request: Request) -> JSONResponse:
        query = self.filter_queryset(request, self.get_queryset())
        try:
            instance = await query.get(pk=self._pk(request))
        except query.model.DoesNotExist:
            return JSONResponse({"detail": "Not found"}, 404)
        await self.check_object_permissions(request, instance)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"detail": "Invalid JSON"}, 400)
        serializer = self.serializer_class(instance, data=payload)
        if not await serializer.ais_valid():
            return JSONResponse(serializer.errors, 400)
        try:
            await serializer.asave()
        except ValidationError as exc:
            return JSONResponse(exc.errors, 400)
        return JSONResponse(serializer.data)

    async def destroy(self, request: Request) -> JSONResponse:
        query = self.filter_queryset(request, self.get_queryset())
        try:
            instance = await query.get(pk=self._pk(request))
        except query.model.DoesNotExist:
            return JSONResponse({"detail": "Not found"}, 404)
        await self.check_object_permissions(request, instance)
        await query.filter(pk=instance.pk).delete()
        return JSONResponse({}, 204)


class Router:
    def __init__(self, application: ASGIApplication) -> None:
        self.application = application

    def register(self, prefix: str, viewset: type[ModelViewSet]) -> None:
        if not prefix or "/" in prefix.strip("/"):
            raise ValueError("A viewset prefix must be one path segment")
        path = "/" + prefix.strip("/")

        def handler(action: str):
            async def view(request: Request) -> JSONResponse:
                instance = viewset()
                instance.action = action
                return await instance.dispatch_action(request, action)
            view.__asyncdjango_viewset__ = viewset
            view.__asyncdjango_action__ = action
            return view

        self.application.add_route(path, handler("list"), methods=("GET",))
        self.application.add_route(path, handler("create"), methods=("POST",))
        detail = path + "/{pk}"
        self.application.add_route(detail, handler("retrieve"), methods=("GET",))
        self.application.add_route(detail, handler("update"), methods=("PUT", "PATCH"))
        self.application.add_route(detail, handler("destroy"), methods=("DELETE",))
