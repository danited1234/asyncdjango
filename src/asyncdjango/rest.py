"""Initial async APIView and model viewset conventions."""

from __future__ import annotations

import json
from typing import Any

from .asgi import ASGIApplication, JSONResponse, Request
from .db.models import IntegerField, Model, QuerySet
from .serializers import ModelSerializer, ValidationError


class APIView:
    async def dispatch(self, request: Request) -> JSONResponse:
        method = getattr(self, request.method.lower(), None)
        if method is None:
            return JSONResponse({"detail": "Method not allowed"}, 405)
        return await method(request)

    @classmethod
    def as_view(cls):
        async def view(request: Request) -> JSONResponse:
            return await cls().dispatch(request)
        return view


class ModelViewSet:
    queryset: QuerySet
    serializer_class: type[ModelSerializer]

    def get_queryset(self) -> QuerySet:
        return self.queryset.all()

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
        objects = await self.get_queryset().fetch_all()
        return JSONResponse(self.serializer_class(objects, many=True).data)

    async def retrieve(self, request: Request) -> JSONResponse:
        query = self.get_queryset()
        try:
            instance = await query.get(pk=self._pk(request))
        except query.model.DoesNotExist:
            return JSONResponse({"detail": "Not found"}, 404)
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
        query = self.get_queryset()
        try:
            instance = await query.get(pk=self._pk(request))
        except query.model.DoesNotExist:
            return JSONResponse({"detail": "Not found"}, 404)
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
        count = await self.get_queryset().filter(pk=self._pk(request)).delete()
        if not count:
            return JSONResponse({"detail": "Not found"}, 404)
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
                return await getattr(viewset(), action)(request)
            return view

        self.application.add_route(path, handler("list"), methods=("GET",))
        self.application.add_route(path, handler("create"), methods=("POST",))
        detail = path + "/{pk}"
        self.application.add_route(detail, handler("retrieve"), methods=("GET",))
        self.application.add_route(detail, handler("update"), methods=("PUT", "PATCH"))
        self.application.add_route(detail, handler("destroy"), methods=("DELETE",))
