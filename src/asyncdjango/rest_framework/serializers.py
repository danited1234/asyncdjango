"""ADRF-compatible serializers whose async API runs natively.

``await serializer.adata``, ``await serializer.ais_valid()`` and
``await serializer.asave()`` run DRF's own synchronous field, validator, and
``create()``/``update()`` code in a native greenlet. Relation fields may read
unloaded relations: those reads become awaited queries on the event loop, so
DRF serializers work unchanged (``select_related``/``prefetch_related`` still
avoid N+1 queries).

Fields may also be async: a ``SerializerMethodField`` whose method is
``async def``, or a field or nested serializer that defines its own
``async def ato_representation()``.

``to_representation()`` of ``Serializer`` and ``ListSerializer`` is adapted
from Django REST framework 3.16 (BSD-3-Clause); see docs/upstream.md.
"""

from __future__ import annotations

from typing import Any

from django.db import models
from rest_framework import serializers as drf
from rest_framework.fields import SkipField
from rest_framework.relations import PKOnlyObject
from rest_framework.serializers import *  # noqa: F403  (fields, relations, ...)
from rest_framework.serializers import (  # noqa: F401  (re-exported like DRF's module)
    LIST_SERIALIZER_KWARGS,
    LIST_SERIALIZER_KWARGS_REMOVE,
    ValidationError,
)
from rest_framework.utils.serializer_helpers import ReturnDict, ReturnList

from ..db.scope import run_native
from ._compat import resolve
from .fields import SerializerMethodField  # must follow the * import: replaces DRF's

_custom_async_classes: dict[type, bool] = {}


def _custom_async_representation(obj: Any) -> Any:
    """Return obj.ato_representation if its class defines its own."""
    cls = type(obj)
    custom = _custom_async_classes.get(cls)
    if custom is None:
        method = getattr(cls, "ato_representation", None)
        custom = method is not None and method not in _DEFAULT_ATO_REPRESENTATIONS
        _custom_async_classes[cls] = custom
    return obj.ato_representation if custom else None


def _represent(field: Any, value: Any) -> Any:
    custom = _custom_async_representation(field)
    if custom is not None:
        return resolve(custom(value))
    return resolve(field.to_representation(value))


class BaseSerializer(drf.BaseSerializer):
    @classmethod
    def many_init(cls, *args: Any, **kwargs: Any) -> Any:
        list_kwargs = {}
        for key in LIST_SERIALIZER_KWARGS_REMOVE:
            value = kwargs.pop(key, None)
            if value is not None:
                list_kwargs[key] = value
        list_kwargs["child"] = cls(*args, **kwargs)
        list_kwargs.update({
            key: value for key, value in kwargs.items() if key in LIST_SERIALIZER_KWARGS
        })
        meta = getattr(cls, "Meta", None)
        list_serializer_class = getattr(meta, "list_serializer_class", ListSerializer)
        return list_serializer_class(*args, **list_kwargs)

    @property
    def adata(self) -> Any:
        """Awaitable serialized data: ``data = await serializer.adata``."""
        return self._adata()

    async def _raw_adata(self) -> Any:
        if hasattr(self, "initial_data") and not hasattr(self, "_validated_data"):
            raise AssertionError(
                "When a serializer is passed a `data` keyword argument you "
                "must call `.is_valid()` before attempting to access the "
                "serialized `.data` representation.\n"
                "You should either call `.is_valid()` first, "
                "or access `.initial_data` instead."
            )
        if not hasattr(self, "_data"):
            if self.instance is not None and not getattr(self, "_errors", None):
                self._data = await self.ato_representation(self.instance)
            elif hasattr(self, "_validated_data") and not getattr(self, "_errors", None):
                self._data = await self.ato_representation(self.validated_data)
            else:
                self._data = self.get_initial()
        return self._data

    async def _adata(self) -> Any:
        return await self._raw_adata()

    async def ato_representation(self, instance: Any) -> Any:
        return await run_native(self.to_representation, instance)

    async def ais_valid(self, *, raise_exception: bool = False) -> bool:
        return await run_native(self.is_valid, raise_exception=raise_exception)

    async def acreate(self, validated_data: Any) -> Any:
        return await run_native(self.create, validated_data)

    async def aupdate(self, instance: Any, validated_data: Any) -> Any:
        return await run_native(self.update, instance, validated_data)

    async def asave(self, **kwargs: Any) -> Any:
        assert hasattr(self, "_errors"), "You must call `.is_valid()` before calling `.asave()`."
        assert not self.errors, "You cannot call `.asave()` on a serializer with invalid data."
        assert "commit" not in kwargs, (
            "'commit' is not a valid keyword argument to the 'asave()' method. "
            "If you need to access data before committing to the database then "
            "inspect 'serializer.validated_data' instead. You can also pass "
            "additional keyword arguments to 'asave()' if you need to set extra "
            "attributes on the saved model instance. For example: "
            "'serializer.asave(owner=request.user)'."
        )
        assert not hasattr(self, "_data"), (
            "You cannot call `.asave()` after accessing `serializer.data`. If you "
            "need to access data before committing to the database then inspect "
            "'serializer.validated_data' instead."
        )
        validated_data = {**self.validated_data, **kwargs}
        if self.instance is not None:
            self.instance = await self.aupdate(self.instance, validated_data)
            assert self.instance is not None, "`aupdate()` did not return an object instance."
        else:
            self.instance = await self.acreate(validated_data)
            assert self.instance is not None, "`acreate()` did not return an object instance."
        return self.instance


class Serializer(BaseSerializer, drf.Serializer):
    async def _adata(self) -> Any:
        return ReturnDict(await self._raw_adata(), serializer=self)

    def to_representation(self, instance: Any) -> Any:
        ret = {}
        for field in self._readable_fields:
            try:
                attribute = field.get_attribute(instance)
            except SkipField:
                continue
            check_for_none = attribute.pk if isinstance(attribute, PKOnlyObject) else attribute
            if check_for_none is None:
                ret[field.field_name] = None
            else:
                ret[field.field_name] = _represent(field, attribute)
        return ret


class ListSerializer(BaseSerializer, drf.ListSerializer):
    async def _adata(self) -> Any:
        return ReturnList(await self._raw_adata(), serializer=self)

    def to_representation(self, data: Any) -> Any:
        iterable = data.all() if isinstance(data, models.manager.BaseManager) else data
        return [_represent(self.child, item) for item in iterable]

    async def asave(self, **kwargs: Any) -> Any:
        assert "commit" not in kwargs, (
            "'commit' is not a valid keyword argument to the 'asave()' method."
        )
        validated_data = [{**attrs, **kwargs} for attrs in self.validated_data]
        if self.instance is not None:
            self.instance = await self.aupdate(self.instance, validated_data)
            assert self.instance is not None, "`aupdate()` did not return an object instance."
        else:
            self.instance = await self.acreate(validated_data)
            assert self.instance is not None, "`acreate()` did not return an object instance."
        return self.instance

    async def acreate(self, validated_data: Any) -> Any:
        return [await self.child.acreate(attrs) for attrs in validated_data]


class ModelSerializer(Serializer, drf.ModelSerializer):
    """DRF ``ModelSerializer``; default ``acreate``/``aupdate`` run ``create``/``update``."""


class HyperlinkedModelSerializer(Serializer, drf.HyperlinkedModelSerializer):
    pass


_DEFAULT_ATO_REPRESENTATIONS = {
    BaseSerializer.ato_representation,
}
_DEFAULT_ATO_REPRESENTATIONS.add(SerializerMethodField.ato_representation)
