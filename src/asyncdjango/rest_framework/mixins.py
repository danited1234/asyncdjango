"""ADRF's async model mixins (``alist``, ``acreate``, ...), run natively."""

from __future__ import annotations

from typing import Any

from rest_framework import mixins, status
from rest_framework.response import Response

from ..db.scope import run_native


async def get_data(serializer: Any) -> Any:
    """``await serializer.adata`` if available, else ``serializer.data`` natively."""
    # Check the class: reading the property would create a coroutine.
    if hasattr(type(serializer), "adata"):
        return await serializer.adata
    return await run_native(lambda: serializer.data)


async def is_valid(serializer: Any, *, raise_exception: bool = False) -> bool:
    if hasattr(serializer, "ais_valid"):
        return await serializer.ais_valid(raise_exception=raise_exception)
    return await run_native(serializer.is_valid, raise_exception=raise_exception)


async def save(serializer: Any, **kwargs: Any) -> Any:
    if hasattr(serializer, "asave"):
        return await serializer.asave(**kwargs)
    return await run_native(serializer.save, **kwargs)


class CreateModelMixin(mixins.CreateModelMixin):
    async def acreate(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)
        await is_valid(serializer, raise_exception=True)
        await self.perform_acreate(serializer)
        data = await get_data(serializer)
        headers = self.get_success_headers(data)
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)

    async def perform_acreate(self, serializer: Any) -> None:
        await save(serializer)


class ListModelMixin(mixins.ListModelMixin):
    async def alist(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        queryset = await self.afilter_queryset(self.get_queryset())
        page = await self.apaginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return await self.get_apaginated_response(await get_data(serializer))
        serializer = self.get_serializer(queryset, many=True)
        return Response(await get_data(serializer))


class RetrieveModelMixin(mixins.RetrieveModelMixin):
    async def aretrieve(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        instance = await self.aget_object()
        serializer = self.get_serializer(instance)
        return Response(await get_data(serializer))


class UpdateModelMixin(mixins.UpdateModelMixin):
    async def aupdate(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        partial = kwargs.pop("partial", False)
        instance = await self.aget_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        await is_valid(serializer, raise_exception=True)
        await self.perform_aupdate(serializer)
        if getattr(instance, "_prefetched_objects_cache", None):
            # The update made the prefetch cache stale.
            instance._prefetched_objects_cache = {}
        return Response(await get_data(serializer))

    async def perform_aupdate(self, serializer: Any) -> None:
        await save(serializer)

    async def partial_aupdate(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        kwargs["partial"] = True
        return await self.aupdate(request, *args, **kwargs)


class DestroyModelMixin(mixins.DestroyModelMixin):
    async def adestroy(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        instance = await self.aget_object()
        await self.perform_adestroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    async def perform_adestroy(self, instance: Any) -> None:
        await instance.adelete()
