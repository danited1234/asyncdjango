# Upstream code and version policy

asyncdjango runs Django's own ORM, transaction, and signal code. It patches a
small, documented set of Django and asgiref entry points and adapts a few
functions from Django and Django REST framework. Those internals change
between minor releases, so the dependency ranges are pinned to the versions
the test suite has verified:

| Dependency | Supported range | Verified with |
| --- | --- | --- |
| Python | 3.12+ (Django 6.0's minimum) | 3.12, 3.13.12 |
| Django | 6.0.x, 5.2.x LTS | 6.0.3, 5.2.18 |
| Django REST framework (optional) | 3.16.x | 3.16.1 |
| psycopg | 3.2+ | 3.3.2 (binary, libpq 16.9), 3.3.6 (binary, libpq 18.4) |
| psycopg-pool | 3.2+ | 3.3.3 |
| greenlet | 3.0+ | 3.5.6 |
| asgiref | Django's requirement | 3.12.1 |
| redis-py (optional) | 5.0+ | 8.1.0 |
| Django Channels (optional, tests only) | 4.2+ | 4.2.2 |
| PostgreSQL | 14+ (Django 6.0's minimum) | 18.6 |

Before widening a range, run the full suite against the new version. Django
5.2 was added after comparing every adapted function below (and the patched
methods' originals) between 5.2.18 and 6.0.3: they are identical apart from
comment wrapping. The adapted code keeps its 6.0.3 provenance.
`tests/test_guards.py` fails if Django adds an async method that still offloads
to a thread, and the other tests exercise every patched code path.

## Patched entry points

Installed from `AsyncDjangoConfig.ready()`:

| Target | Change | Condition |
| --- | --- | --- |
| `django.db.utils.ConnectionHandler.__getitem__` | Returns the active native scope's wrapper | Always; inert outside a scope |
| `django.dispatch.Signal.send` / `send_robust` | Awaits async receivers through the greenlet bridge | Only inside a native greenlet |
| `asgiref.sync.AsyncToSync.__call__` | Awaits directly through the greenlet bridge | Only inside a native greenlet |
| `QuerySet`, `RawQuerySet`, `Model` async methods, `aprefetch_related_objects` | Native implementations | `PATCH_ORM` |
| Related-manager factories (`create_reverse_many_to_one_manager`, `create_forward_many_to_many_manager`, `create_generic_related_manager`) | Native async methods on the generated classes | `PATCH_ORM` |
| `django.db.transaction.Atomic` | Adds `__aenter__`/`__aexit__` and `async def` decoration | `PATCH_ORM` |
| `django.contrib.sessions.backends.db.SessionStore.asave` (also used by `cached_db`) | Native transaction instead of `sync_to_async` | `PATCH_ORM` and `django.contrib.sessions` installed |
| `django.utils.deprecation.MiddlewareMixin.__acall__` | Runs `process_request`/`process_response` natively | Only for classes on the native middleware list |

`asyncdjango/_threads.py` contains the only deliberate thread offloads
(error reporting for 5xx responses, finishing requests that already used
Django's per-request thread, and ORM calls on a loop that `async_to_sync()`
made for a blocked synchronous caller); the source guard test allows that
file alone.

## Internals read

| Internal | Used for |
| --- | --- |
| `asgiref.sync.AsyncToSync.loop_thread_executors` | Recognizing an event loop that `async_to_sync()` created for a synchronous caller (`scope.in_sync_caller_loop()`). asgiref registers each such loop there for the duration of the call. |
| `django.core.cache.backends.redis.RedisCacheClient` (`_servers`, `_pool_options`, `_get_connection_pool_index()`, `get_client()`) and `RedisCache._class` | `asyncdjango.cache` subclasses Django's Redis backend and only swaps the client that `get_client()` returns inside native code. |
| `BaseDatabaseWrapper._commit()`, `_rollback()`, `_savepoint_rollback()` | `SESSION_SQL` wraps them on each connection (from `connection_created`) to learn when a `SET` sent inside a transaction was kept or undone. |
| `psycopg_pool.AsyncConnectionPool` shutdown order | `close()` waits for the pool's worker tasks before closing idle connections; under `asyncio.run()` shutdown those workers are already cancelled, so `pool._close_at_shutdown()` closes every connection the pool opened itself (tracked from the `configure` callback). |

`NativeASGIHandler.load_middleware()` also swaps
`django.core.handlers.base.convert_exception_to_response` while it builds
the middleware chain, under a lock, and restores it afterwards.

## Adapted source

Each adaptation keeps the upstream logic and changes only how the steps that
used `sync_to_async` are executed. Their licenses are in `licenses/`, and
`pyproject.toml` ships them with distributions.

| Upstream (tag) | Original | Local destination | Change |
| --- | --- | --- | --- |
| Django 6.0.3 | `django/core/handlers/asgi.py`, `ASGIHandler.handle()` | `asyncdjango/asgi.py`, `NativeASGIHandler.handle()` | Signals and `response.close()` run natively unless the request used Django's per-request thread |
| Django 6.0.3 | `django/core/handlers/base.py`, `BaseHandler.get_response_async()`, `_get_response_async()`, `make_view_atomic()` | `asyncdjango/asgi.py` | Rendering, exception middleware, and logging run natively; `ATOMIC_REQUESTS` supports async views |
| Django 6.0.3 | `django/core/handlers/exception.py`, `convert_exception_to_response()` | `asyncdjango/asgi.py`, `_native_convert_exception_to_response()` | `response_for_exception()` runs natively |
| Django 6.0.3 | `django/dispatch/dispatcher.py`, `Signal.send()`, `send_robust()`, `asend()` | `asyncdjango/signals.py` | Async receivers awaited via the bridge; sync receivers of `asend` run natively |
| Django 6.0.3 | `django/db/backends/postgresql/base.py`, `get_new_connection()`, `create_cursor()`, `_configure_connection()`, `CursorMixin.callproc()` | `asyncdjango/db/backend.py`, `asyncdjango/db/pool.py` | Async psycopg connections from a pool |
| Django 6.0.3 | `django/contrib/sessions/backends/db.py`, `SessionStore.asave()` | `asyncdjango/db/patch.py`, `_native_session_asave()` | `async with transaction.atomic()` and `await obj.asave()` instead of a `sync_to_async` transaction |
| DRF 3.16.1 | `rest_framework/views.py`, `APIView.dispatch()`, `check_permissions()`, `check_object_permissions()`, `check_throttles()` | `asyncdjango/rest_framework/views.py` | Async dispatch; async hook results awaited |
| DRF 3.16.1 | `rest_framework/request.py`, `Request._authenticate()` | `asyncdjango/rest_framework/requests.py` | Async authenticators awaited |
| DRF 3.16.1 | `rest_framework/serializers.py`, `BaseSerializer.many_init()`, `Serializer.to_representation()`, `ListSerializer.to_representation()` | `asyncdjango/rest_framework/serializers.py` | Default list class; async field representations awaited |
| DRF 3.16.1 | `rest_framework/generics.py`, `GenericAPIView.filter_queryset()` | `asyncdjango/rest_framework/generics.py` | Async filter backends awaited |
| DRF 3.16.1 | `rest_framework/decorators.py`, `api_view()` | `asyncdjango/rest_framework/decorators.py` | Async handlers |

The ADRF-compatible public names (`adata`, `aget_object`, `perform_acreate`,
the router's async action map, ...) follow adrf 0.1.12's API; no adrf source
is included and adrf is not a dependency.

This is a compliance checklist, not legal advice. asyncdjango itself is
released under the BSD 3-Clause license (`LICENSE`), the same license as
Django and DRF.
