"""Replace Django's thread-offloading async ORM methods with native ones.

Django implements methods such as ``QuerySet.aget()`` and ``Model.asave()`` as
``await sync_to_async(self.get)(...)``: the synchronous method runs on a
worker thread. The replacements installed here call the same synchronous
method through ``run_native()`` instead, so Django's exact semantics (signals,
cascades, savepoints, custom ``save()`` overrides) are kept while I/O is
awaited on the event loop.

Each replacement checks the target alias first. A non-PostgreSQL alias raises
``NativeAsyncUnsupported``, or with ``ALLOW_SYNC_FALLBACK`` calls Django's
original thread-based method and emits ``SyncFallbackWarning``.

On an event loop that ``async_to_sync()`` created for a synchronous caller
(a Celery task, for example) the replacements call Django's original method,
which runs on that caller's thread; see ``scope.in_sync_caller_loop()``.
"""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable
from itertools import islice
from typing import Any

from django.db import router
from django.db.models import Model
from django.db.models import query as query_module
from django.db.models.fields import related_descriptors

from .. import conf
from ..exceptions import NativeAsyncUnsupported
from .pool import is_native_alias
from .scope import (
    NativeScope,
    _current_scope,
    active_scope,
    in_sync_caller_loop,
    run_in_scope,
    run_native,
)

QUERYSET_METHODS = {
    "aaggregate": "aggregate",
    "abulk_create": "bulk_create",
    "abulk_update": "bulk_update",
    "acontains": "contains",
    "acount": "count",
    "acreate": "create",
    "adelete": "delete",
    "aearliest": "earliest",
    "aexists": "exists",
    "aexplain": "explain",
    "afirst": "first",
    "aget": "get",
    "aget_or_create": "get_or_create",
    "ain_bulk": "in_bulk",
    "alast": "last",
    "alatest": "latest",
    "aupdate": "update",
    "aupdate_or_create": "update_or_create",
}
QUERYSET_WRITE_METHODS = {
    "abulk_create", "abulk_update", "acreate", "adelete", "aget_or_create",
    "aupdate", "aupdate_or_create",
}
MODEL_METHODS = {"asave": "save", "adelete": "delete", "arefresh_from_db": "refresh_from_db"}
RELATED_MANAGER_METHODS = {
    "aadd": "add",
    "aclear": "clear",
    "acreate": "create",
    "aget_or_create": "get_or_create",
    "aremove": "remove",
    "aset": "set",
    "aupdate_or_create": "update_or_create",
}


class SyncFallbackWarning(RuntimeWarning):
    """A native async ORM call fell back to Django's worker-thread version."""


_originals: list[tuple[Any, str, Any, bool]] = []


def _set(owner: Any, name: str, value: Any) -> None:
    had = name in vars(owner)
    _originals.append((owner, name, vars(owner).get(name), had))
    setattr(owner, name, value)


def _queryset_aliases(queryset: Any, write: bool) -> set[str]:
    aliases = {queryset.db}
    if write and queryset._db is None:
        aliases.add(router.db_for_write(queryset.model, **queryset._hints))
    return aliases


def _model_aliases(instance: Model, name: str, kwargs: dict[str, Any]) -> set[str]:
    using = kwargs.get("using")
    if using:
        return {using}
    if name == "arefresh_from_db":
        return {router.db_for_read(instance.__class__, instance=instance)}
    return {router.db_for_write(instance.__class__, instance=instance)}


def _manager_aliases(manager: Any) -> set[str]:
    return {manager.db, router.db_for_write(manager.model, instance=manager.instance)}


async def _fallback(original: Callable[..., Any], aliases: set[str], args: tuple,
                    kwargs: dict[str, Any]) -> Any:
    unsupported = sorted(alias for alias in aliases if not is_native_alias(alias))
    message = (
        f"{original.__qualname__} uses database alias(es) {unsupported} that the "
        "native backend does not serve (psycopg 3 PostgreSQL only)"
    )
    if not conf.get("ALLOW_SYNC_FALLBACK"):
        raise NativeAsyncUnsupported(message)
    warnings.warn(message + "; falling back to a worker thread", SyncFallbackWarning,
                  stacklevel=3)
    return await original(*args, **kwargs)


def _native(async_name: str, sync_name: str, original: Callable[..., Any],
            aliases: Callable[..., set[str]]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        if in_sync_caller_loop():
            return await original(self, *args, **kwargs)
        targets = aliases(self, kwargs)
        if not all(is_native_alias(alias) for alias in targets):
            return await _fallback(original, targets, (self, *args), kwargs)
        return await run_native(getattr(self, sync_name), *args, **kwargs)

    method.__asyncdjango_native__ = True
    return method


def _native_queryset_aiter(original: Callable[..., Any]) -> Callable[..., Any]:
    def __aiter__(self: Any) -> Any:
        if in_sync_caller_loop():
            return original(self)
        if not is_native_alias(self.db):
            if not conf.get("ALLOW_SYNC_FALLBACK"):
                raise NativeAsyncUnsupported(
                    f"async iteration uses database alias {self.db!r} that the "
                    "native backend does not serve"
                )
            warnings.warn("async iteration falling back to a worker thread",
                          SyncFallbackWarning, stacklevel=2)
            return original(self)

        async def generator() -> Any:
            await run_native(self._fetch_all)
            for item in self._result_cache:
                yield item

        return generator()

    __aiter__.__asyncdjango_native__ = True
    __aiter__.__wrapped__ = original
    return __aiter__


def _native_aiterator(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def aiterator(self: Any, chunk_size: int = 2000) -> Any:
        """Stream rows natively, using a server-side cursor when enabled."""
        if chunk_size <= 0:
            raise ValueError("Chunk size must be strictly positive.")
        if in_sync_caller_loop():
            async for item in original(self, chunk_size=chunk_size):
                yield item
            return
        if not is_native_alias(self.db):
            if not conf.get("ALLOW_SYNC_FALLBACK"):
                raise NativeAsyncUnsupported(
                    f"aiterator() uses database alias {self.db!r} that the "
                    "native backend does not serve"
                )
            warnings.warn("aiterator() falling back to a worker thread",
                          SyncFallbackWarning, stacklevel=2)
            async for item in original(self, chunk_size=chunk_size):
                yield item
            return
        from django.db import connections

        use_chunked_fetch = not connections.settings[self.db].get(
            "DISABLE_SERVER_SIDE_CURSORS"
        )
        # Keep one connection for the whole iteration (the server-side cursor
        # lives on it) and let the consumer's own ORM calls between rows use it
        # too, as one thread's connection does in sync Django. Inside atomic()
        # the transaction's scope is reused.
        scope = active_scope()
        token = None
        if scope is None:
            scope = NativeScope()
            token = _current_scope.set(scope)
        iterable = self._iterable_class(
            self, chunked_fetch=use_chunked_fetch, chunk_size=chunk_size
        )
        prefetch = self._prefetch_related_lookups
        iterator = None
        try:
            # Some iterables execute their query in __iter__(), so create the
            # iterator natively too.
            iterator = await run_in_scope(scope, iter, iterable)
            while True:
                chunk = await run_in_scope(scope, list, islice(iterator, chunk_size))
                if prefetch and chunk:
                    await run_in_scope(
                        scope, query_module.prefetch_related_objects, chunk, *prefetch
                    )
                for item in chunk:
                    yield item
                if len(chunk) < chunk_size:
                    break
        finally:
            # May run in the loop's async-generator finalizer task when the
            # consumer stopped early; nothing here depends on the current task.
            try:
                close = getattr(iterator, "close", None)
                if close is not None:
                    await run_in_scope(scope, close)
            finally:
                if token is not None:
                    try:
                        _current_scope.reset(token)
                    except ValueError:
                        pass  # finalized elsewhere; the closed scope is ignored
                    await scope.aclose_uncancelled()

    aiterator.__asyncdjango_native__ = True
    return aiterator


async def aprefetch_related_objects(model_instances: Any, *related_lookups: Any) -> None:
    """Native replacement for ``django.db.models.aprefetch_related_objects``."""
    await run_native(query_module.prefetch_related_objects, model_instances,
                     *related_lookups)


def _patch_related_manager_class(cls: type) -> type:
    for async_name, sync_name in RELATED_MANAGER_METHODS.items():
        original = vars(cls).get(async_name)
        if original is None or getattr(original, "__asyncdjango_native__", False):
            continue
        setattr(cls, async_name, _native(
            async_name, sync_name, original,
            lambda manager, kwargs: _manager_aliases(manager),
        ))
    return cls


def _wrap_manager_factory(module: Any, name: str) -> None:
    factory = getattr(module, name)

    @functools.wraps(factory)
    def create(*args: Any, **kwargs: Any) -> type:
        return _patch_related_manager_class(factory(*args, **kwargs))

    _set(module, name, create)


def _native_session_asave(original: Callable[..., Any]) -> Callable[..., Any]:
    """``SessionStore.asave()`` for the database backends, without a thread.

    Adapted from Django 6.0.3's ``django.contrib.sessions.backends.db``
    (BSD-3-Clause; see docs/upstream.md), which wraps the transaction in
    ``sync_to_async`` because Django's ``atomic()`` is not an async context
    manager. ``cached_db`` reaches this through ``super().asave()`` and keeps
    its own cache write.
    """
    from django.contrib.sessions.backends.base import CreateError, UpdateError
    from django.db import DatabaseError, IntegrityError, transaction

    @functools.wraps(original)
    async def asave(self: Any, must_create: bool = False) -> Any:
        if in_sync_caller_loop():
            return await original(self, must_create)
        if self.session_key is None:
            return await self.acreate()
        data = await self._aget_session(no_load=must_create)
        obj = await self.acreate_model_instance(data)
        using = router.db_for_write(self.model, instance=obj)
        if not is_native_alias(using):
            return await _fallback(original, {using}, (self, must_create), {})
        try:
            async with transaction.atomic(using=using):
                await obj.asave(force_insert=must_create, force_update=not must_create,
                                using=using)
        except IntegrityError as exc:
            if must_create:
                raise CreateError from exc
            raise
        except DatabaseError as exc:
            if not must_create:
                raise UpdateError from exc
            raise

    asave.__asyncdjango_native__ = True
    return asave


def native_queryset_methods() -> dict[str, Any]:
    """Native replacements for Django's QuerySet async methods, by name."""
    QuerySet = query_module.QuerySet
    methods: dict[str, Any] = {}
    for async_name, sync_name in QUERYSET_METHODS.items():
        write = async_name in QUERYSET_WRITE_METHODS
        methods[async_name] = _native(
            async_name, sync_name, vars(QuerySet)[async_name]
            if not is_native(vars(QuerySet)[async_name])
            else vars(QuerySet)[async_name].__wrapped__,
            lambda qs, kwargs, write=write: _queryset_aliases(qs, write),
        )
    original_aiter = vars(QuerySet)["__aiter__"]
    methods["__aiter__"] = _native_queryset_aiter(
        getattr(original_aiter, "__wrapped__", original_aiter))
    original_aiterator = vars(QuerySet)["aiterator"]
    methods["aiterator"] = _native_aiterator(
        getattr(original_aiterator, "__wrapped__", original_aiterator))
    return methods


def native_model_methods() -> dict[str, Any]:
    """Native replacements for Model.asave(), adelete(), arefresh_from_db()."""
    methods: dict[str, Any] = {}
    for async_name, sync_name in MODEL_METHODS.items():
        original = vars(Model)[async_name]
        original = original.__wrapped__ if is_native(original) else original
        methods[async_name] = _native(
            async_name, sync_name, original,
            lambda obj, kwargs, name=async_name: _model_aliases(obj, name, kwargs),
        )
    return methods


_installed = False


def install() -> None:
    """Install the native methods. Idempotent."""
    global _installed
    if _installed:
        return
    QuerySet = query_module.QuerySet
    for name, method in native_queryset_methods().items():
        _set(QuerySet, name, method)
    _set(query_module.RawQuerySet, "__aiter__",
         _native_queryset_aiter(query_module.RawQuerySet.__aiter__))
    for name, method in native_model_methods().items():
        _set(Model, name, method)
    import django.db.models as models_package

    _set(query_module, "aprefetch_related_objects", aprefetch_related_objects)
    _set(models_package, "aprefetch_related_objects", aprefetch_related_objects)
    _wrap_manager_factory(related_descriptors, "create_reverse_many_to_one_manager")
    _wrap_manager_factory(related_descriptors, "create_forward_many_to_many_manager")
    try:
        from django.contrib.contenttypes import fields as contenttypes_fields
    except ImportError:  # pragma: no cover - contenttypes is optional
        pass
    else:
        _wrap_manager_factory(contenttypes_fields, "create_generic_related_manager")
    from django.apps import apps

    if apps.is_installed("django.contrib.sessions"):
        from django.contrib.sessions.backends import db as session_db

        _set(session_db.SessionStore, "asave",
             _native_session_asave(vars(session_db.SessionStore)["asave"]))
    from django.db import transaction as django_transaction

    from .transaction import Atomic

    for name in ("__aenter__", "__aexit__", "__call__"):
        _set(django_transaction.Atomic, name, vars(Atomic)[name])
    _installed = True


def uninstall() -> None:
    """Restore Django's original methods (used by tests)."""
    global _installed
    while _originals:
        owner, name, value, had = _originals.pop()
        if had:
            setattr(owner, name, value)
        else:
            delattr(owner, name)
    _installed = False


def is_native(method: Any) -> bool:
    return getattr(method, "__asyncdjango_native__", False)
