from __future__ import annotations

from typing import Any

from django.shortcuts import aget_object_or_404  # noqa: F401  (ADRF compatibility)
from rest_framework import generics as drf_generics

from ..db.scope import run_native
from . import mixins
from ._compat import resolve
from .views import APIView


class GenericAPIView(APIView, drf_generics.GenericAPIView):
    """DRF ``GenericAPIView`` with ADRF's async helpers, run natively.

    Filter backends and paginators may define ``afilter_queryset()`` or
    ``apaginate_queryset()`` as ``async def``; otherwise DRF's synchronous
    methods run in a native greenlet with their queries awaited.
    """

    def filter_queryset(self, queryset: Any) -> Any:
        for backend_class in list(self.filter_backends):
            backend = backend_class()
            afilter = getattr(backend, "afilter_queryset", None)
            if afilter is not None:
                queryset = resolve(afilter(self.request, queryset, self))
            else:
                queryset = backend.filter_queryset(self.request, queryset, self)
        return queryset

    def paginate_queryset(self, queryset: Any) -> Any:
        if self.paginator is None:
            return None
        apaginate = getattr(self.paginator, "apaginate_queryset", None)
        if apaginate is not None:
            return resolve(apaginate(queryset, self.request, view=self))
        return self.paginator.paginate_queryset(queryset, self.request, view=self)

    def get_paginated_response(self, data: Any) -> Any:
        assert self.paginator is not None
        return resolve(self.paginator.get_paginated_response(data))

    async def aget_object(self) -> Any:
        return await run_native(self.get_object)

    async def afilter_queryset(self, queryset: Any) -> Any:
        return await run_native(self.filter_queryset, queryset)

    async def apaginate_queryset(self, queryset: Any) -> Any:
        return await run_native(self.paginate_queryset, queryset)

    async def get_apaginated_response(self, data: Any) -> Any:
        return await run_native(self.get_paginated_response, data)


class CreateAPIView(mixins.CreateModelMixin, GenericAPIView):
    async def post(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.acreate(request, *args, **kwargs)


class ListAPIView(mixins.ListModelMixin, GenericAPIView):
    async def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.alist(request, *args, **kwargs)


class RetrieveAPIView(mixins.RetrieveModelMixin, GenericAPIView):
    async def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aretrieve(request, *args, **kwargs)


class DestroyAPIView(mixins.DestroyModelMixin, GenericAPIView):
    async def delete(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.adestroy(request, *args, **kwargs)


class UpdateAPIView(mixins.UpdateModelMixin, GenericAPIView):
    async def put(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aupdate(request, *args, **kwargs)

    async def patch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.partial_aupdate(request, *args, **kwargs)


class ListCreateAPIView(mixins.ListModelMixin, mixins.CreateModelMixin, GenericAPIView):
    async def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.alist(request, *args, **kwargs)

    async def post(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.acreate(request, *args, **kwargs)


class RetrieveUpdateAPIView(mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                            GenericAPIView):
    async def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aretrieve(request, *args, **kwargs)

    async def put(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aupdate(request, *args, **kwargs)

    async def patch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.partial_aupdate(request, *args, **kwargs)


class RetrieveDestroyAPIView(mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                             GenericAPIView):
    async def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aretrieve(request, *args, **kwargs)

    async def delete(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.adestroy(request, *args, **kwargs)


class RetrieveUpdateDestroyAPIView(mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                                   mixins.DestroyModelMixin, GenericAPIView):
    async def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aretrieve(request, *args, **kwargs)

    async def put(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.aupdate(request, *args, **kwargs)

    async def patch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.partial_aupdate(request, *args, **kwargs)

    async def delete(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.adestroy(request, *args, **kwargs)
