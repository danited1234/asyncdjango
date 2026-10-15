# Porting an ADRF code base

This guide assumes a Django 6.0 project on PostgreSQL with psycopg 3, using
adrf views and serializers and `sync_to_async` around ORM code, which is the
shape of the CRM back end. Work on a branch and run your test suite after
each step.

## 1. Install the app and the ASGI handler

```python
INSTALLED_APPS = [..., "asyncdjango"]

ASYNCDJANGO = {
    "POOL": {"min_size": 2, "max_size": 20},
}
```

```python
# project/asgi.py
from asyncdjango.asgi import get_asgi_application

application = get_asgi_application()
```

From this point every `await X.objects.aget()`, `asave()`, `async for`, and
so on is native. If the project uses Channels, route the `"http"` scope to
this application.

Run `python manage.py check`. Look at the `asyncdjango.W002` warnings, which
list middleware that still runs in a thread (see step 5).

## 2. Replace adrf imports

| adrf | asyncdjango |
| --- | --- |
| `adrf.views.APIView` | `asyncdjango.rest_framework.views.APIView` |
| `adrf.generics.*` | `asyncdjango.rest_framework.generics.*` |
| `adrf.mixins.*` | `asyncdjango.rest_framework.mixins.*` |
| `adrf.viewsets.*` | `asyncdjango.rest_framework.viewsets.*` |
| `adrf.serializers.Serializer`, `ModelSerializer`, `ListSerializer` | `asyncdjango.rest_framework.serializers.*` |
| `adrf.fields.SerializerMethodField` | `asyncdjango.rest_framework.serializers.SerializerMethodField` |
| `adrf.routers.SimpleRouter`, `DefaultRouter` | `asyncdjango.rest_framework.routers.*` |
| `adrf.decorators.api_view` | `asyncdjango.rest_framework.decorators.api_view` |
| `adrf.requests.AsyncRequest` | `asyncdjango.rest_framework.requests.AsyncRequest` |
| `adrf.shortcuts.aget_object_or_404` | `django.shortcuts.aget_object_or_404` |

The method names stay the same: `await serializer.adata`, `asave()`,
`acreate()`, `aupdate()`, `await self.aget_object()`, `apaginate_queryset()`,
`get_apaginated_response()`, `perform_acreate()`, `alist`, `aretrieve`, and so
on. `asyncdjango.rest_framework.serializers` also re-exports DRF's fields and
relations, so `serializers.CharField` keeps working.

Serializers additionally gain `await serializer.ais_valid(raise_exception=...)`.

As in adrf, async viewsets and generic views call the async hooks
(`perform_acreate`, `perform_aupdate`, `perform_adestroy`, `alist`, ...). A
DRF-style `perform_create()` or synchronous `list()` override on such a view is
not called; rename it to the async hook. `ListSerializer.asave()` calls the
child's `acreate()` per item, so a bulk `ListSerializer.create()` override
needs an `acreate()` counterpart.

## 3. Replace `sync_to_async` around ORM code

Sort each `sync_to_async` call by what the wrapped function does:

| Wrapped code | Replace with |
| --- | --- |
| `serializer.is_valid` | `await serializer.ais_valid()` |
| `lambda: serializer.data` | `await serializer.adata` |
| Only ORM access (queries, lazy relations, `save()`) | `await sync_to_native(fn)(...)` |
| Blocking non-database I/O (HTTP, boto3, email, file system, sync Redis) | keep `sync_to_async` |
| Mixed | split it, or keep `sync_to_async` |

```python
# before
is_valid = await sync_to_async(serializer.is_valid)()
# after
is_valid = await serializer.ais_valid()
```

```python
from asyncdjango import sync_to_native

# before
report = await sync_to_async(build_report)(organization)
# after, if build_report only reads the database
report = await sync_to_native(build_report)(organization)
```

`sync_to_native` runs on the event loop: a blocking call inside it stalls
every request on the worker. Keep `sync_to_async` wherever in doubt; it
remains correct, just slower. Remember that `sync_to_async` code uses
Django's per-thread connection, so it cannot see an open native
`transaction.atomic()` block, and vice versa.

## 4. Review synchronous hooks

On the native path these run on the event loop instead of in adrf's thread:
authentication classes, permission and throttle classes, filter backends,
paginators, serializer fields, `validate_*` methods, model `save()`/`delete()`
overrides, and signal receivers. For each one, check that it does not block
on I/O other than the database:

- **JWT authentication** that wraps an async helper in `async_to_sync` (like
  `myproject.middleware.JWTAuthentication`) works unchanged. Simpler is to
  make `authenticate()` an `async def`.
- **Throttles** using Django's cache with a synchronous Redis client block the
  loop for each round trip. Write an async throttle (`async def
  allow_request`) on an async Redis client, or accept the short stall.
- **Signal receivers** that send email or call webhooks should hand the work
  to Celery (as `transaction.on_commit(lambda: task.delay(...))`), which also
  fixes the transaction-ordering problem.
- **N+1 queries.** Serializers that read relations without `select_related`
  now work (each read is an awaited query), but they are still N+1. Add
  `select_related`/`prefetch_related` as before.

`asyncdjango.diagnostics.EventLoopBlockDetector` fails a test when the loop
stalls longer than a threshold, which helps find remaining blocking calls.

## 5. Middleware

Django's built-in middleware (`CommonMiddleware`, `GZipMiddleware`, `SecurityMiddleware`,
`CsrfViewMiddleware`, ...) runs natively; under plain Django each costs one or
two thread hops per request. Custom `MiddlewareMixin` subclasses and sync-only
middleware still run in Django's per-request thread. Options:

- Use an async-capable version where one exists. Django's own middleware and
  `django-cors-headers` 4 are async-capable.
- Middleware that defines an `__acall__` path must also declare
  `sync_capable = True` and `async_capable = True`. Without the flag Django
  treats the class as sync-only and never calls `__acall__`. The CRM's
  `RequestIDMiddleware` and `StructuredLoggingMiddleware` currently lack it.
- Middleware whose only I/O is the database can be listed in
  `ASYNCDJANGO["NATIVE_SYNC_MIDDLEWARE"]` to run natively.
- WhiteNoise is sync-only and reads files, so do not list it. Serve static files from a
  CDN, object storage, or the reverse proxy in production, or accept the
  thread hop for those requests.

## 6. Settings

- `CONN_MAX_AGE` no longer matters for native queries; the pool handles reuse
  (see [deployment.md](deployment.md)). Keep `CONN_MAX_AGE = 0` while any
  synchronous view or thread-bound middleware remains.
- Size the pool: `workers × POOL.max_size` plus Celery and management commands
  must stay under PostgreSQL's `max_connections`, or under PgBouncer's pool.
- Tenancy via `search_path`: an execute wrapper installed from
  `connection_created` keeps working. It runs on every pool checkout, so
  outside a transaction each awaited ORM call may re-issue `SET search_path`.
  Inside `async with transaction.atomic()` it is set once.

## 7. Tests

Django's `TestCase` (and DRF's `APITestCase`) wrap each test in a transaction
on the synchronous connection, which native queries cannot see. For async
tests use:

```python
from asyncdjango.test import NativeTestCase

class OrderTests(NativeTestCase):
    async def test_create(self):
        order = await Order.objects.acreate(total=10)
        ...
```

`NativeTransactionTestCase` commits for real and flushes afterwards; use it for
concurrency, `on_commit`, or code under test that opens its own transactions
in other tasks. Synchronous tests can stay on Django's `TestCase`.

## 8. Roll out

`PATCH_ORM` applies to the whole process. To roll out gradually instead, set
`ASYNCDJANGO = {"PATCH_ORM": False}` and opt models in:

```python
from asyncdjango.db.models import NativeManager, NativeModelMixin

class Order(NativeModelMixin, models.Model):
    objects = NativeManager()
```

With `PATCH_ORM` off, related-manager methods (`aadd`, `aset`, ...) keep
Django's thread-based behavior, and `django.db.transaction.atomic` does not
support `async with`; use `asyncdjango.db.transaction.atomic` for native
transactions.

Celery workers and management commands are unaffected: they use Django's
synchronous ORM as before.
