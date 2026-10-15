from __future__ import annotations

import functools
from typing import Any

from asgiref.sync import iscoroutinefunction
from django.utils.decorators import classonlymethod
from django.utils.functional import classproperty
from rest_framework import viewsets as drf_viewsets

from . import mixins
from .generics import GenericAPIView
from .views import APIView

ASYNC_ACTIONS = ("alist", "acreate", "aretrieve", "aupdate", "partial_aupdate", "adestroy")


class ViewSetMixin(drf_viewsets.ViewSetMixin):
    """DRF ``ViewSetMixin`` whose ``as_view()`` returns an async view when
    any action is ``async def`` (ADRF's rule)."""

    @classproperty
    def view_is_async(cls) -> bool:
        names = (*ASYNC_ACTIONS, *cls.http_method_names)
        if any(iscoroutinefunction(getattr(cls, name, None)) for name in names):
            return True
        return any(iscoroutinefunction(action) for action in cls.get_extra_actions())

    @classonlymethod
    def as_view(cls, actions: dict[str, str] | None = None, **initkwargs: Any) -> Any:
        view = super().as_view(actions, **initkwargs)
        if not cls.view_is_async:
            return view

        async def async_view(request: Any, *args: Any, **kwargs: Any) -> Any:
            return await view(request, *args, **kwargs)

        # Keeps cls, initkwargs, actions, csrf_exempt, ... set by DRF.
        functools.update_wrapper(async_view, view)
        return async_view


class ViewSet(ViewSetMixin, APIView):
    pass


class GenericViewSet(ViewSetMixin, GenericAPIView):
    pass


class ReadOnlyModelViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericViewSet):
    pass


class ModelViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                   mixins.UpdateModelMixin, mixins.DestroyModelMixin,
                   mixins.ListModelMixin, GenericViewSet):
    pass
