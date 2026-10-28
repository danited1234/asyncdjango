# asyncdjango user guide

This guide covers building a Django project with asyncdjango: setup, async
views and the ORM, calling other async services, middleware, the REST layer,
signals, tests, and troubleshooting. For the reasons behind the design, see
the [README](../README.md); for the exact list of differences from Django,
see [compatibility.md](compatibility.md).

**Contents**

1. [What you get](#1-what-you-get)
2. [Setup](#2-setup)
3. [Async views and the ORM](#3-async-views-and-the-orm)
4. [Transactions](#4-transactions)
5. [Calling other async services (Redis, HTTP, ...)](#5-calling-other-async-services)
6. [Synchronous code: `sync_to_native` or `sync_to_async`](#6-synchronous-code)
7. [Middleware](#7-middleware)
8. [REST APIs](#8-rest-apis)
9. [Signals](#9-signals)
10. [Testing](#10-testing)
11. [Settings reference](#11-settings-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. What you get

asyncdjango is a Django app. Once it's in `INSTALLED_APPS`:

- Django's async ORM API (`aget`, `acreate`, `asave`, `async for`, `aiterator`,
  `async with transaction.atomic()`, and more) awaits PostgreSQL through
  psycopg 3's async driver instead of running each call in a worker thread.
- The ASGI handler runs Django's request cycle, including Django's built-in
  middleware, on the event loop.
- `asyncdjango.rest_framework` provides adrf's API (views, generics, viewsets,
  serializers) with every hook running natively.

Your models, managers, querysets, migrations, admin, and settings remain
Django's own. Everything happens on one thread per worker process, like
FastAPI or any other asyncio application.

## 2. Setup

### Requirements

Python 3.12+, Django 6.0.x or 5.2.x LTS, PostgreSQL 14+ with psycopg 3, and optionally
Django REST framework 3.16.x. Other databases can stay configured but are not
served natively (see [§12](#12-troubleshooting)).

### Install

```console
pip install "asyncdjango[drf] @ git+https://github.com/danited1234/asyncdjango.git@main"
pip install "psycopg[binary]"      # 3.3.6+ bundles libpq 18: thread-free query cancel
```

Pin a commit instead of `main` in `requirements.txt`
(`asyncdjango[drf] @ git+https://github.com/danited1234/asyncdjango.git@<commit-sha>`).
From a local checkout, use `pip install -e "path/to/asyncdjango[drf]"`.

### Settings

```python
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # ...
    "rest_framework",     # if you use the REST layer
    "asyncdjango",
    "myapp",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "myapp",
        "USER": "myapp",
        "PASSWORD": "...",
        "HOST": "10.0.0.5",   # an IP avoids a DNS lookup per new connection
        "PORT": "5432",
    }
}

ASYNCDJANGO = {
    "POOL": {"min_size": 2, "max_size": 20},   # per worker process
}
```

### ASGI entry point

```python
# myproject/asgi.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

from asyncdjango.asgi import get_asgi_application

application = get_asgi_application()
```

With Django Channels, give the handler both the `http` and the `lifespan`
scope. `ProtocolTypeRouter` rejects scope types it has no entry for, so
without `"lifespan"` the pools are not opened at startup or closed at
shutdown (they then open on first use):

```python
# myproject/asgi.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

from asyncdjango.asgi import get_asgi_application

django_app = get_asgi_application()  # sets up Django before consumers import models

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from myapp.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_app,
    "lifespan": django_app,
    "websocket": URLRouter(websocket_urlpatterns),
})
```

Async consumers can use the native ORM directly (`await
Room.objects.aget(...)`, `async with transaction.atomic()`), as can
authentication middleware in front of them. Channels' own
`database_sync_to_async` keeps running in a thread; replace it with
`sync_to_native` for ORM-only functions.

### Run

```console
uvicorn myproject.asgi:application --workers 4
gunicorn myproject.asgi:application -k uvicorn.workers.UvicornWorker --workers 4
```

Use one worker per CPU core. Each worker handles many requests concurrently.
At startup the handler opens the connection pools and loads content types
into Django's cache; at shutdown it closes the pools.

On Windows, psycopg's async mode needs a `SelectorEventLoop`. Develop under
WSL or Docker, or make sure your server uses a selector loop. Linux and macOS
need nothing special.

`python manage.py check` reports anything that still runs in a thread (see
[§7](#7-middleware)).

## 3. Async views and the ORM

Write views as `async def`. The ORM API is Django's:

```python
from django.http import JsonResponse
from django.shortcuts import aget_object_or_404

from .models import Customer, Order


async def customer_orders(request, pk):
    customer = await aget_object_or_404(Customer, pk=pk)
    orders = [
        {"id": order.id, "total": str(order.total)}
        async for order in Order.objects.filter(customer=customer).order_by("-created")[:50]
    ]
    count = await Order.objects.filter(customer=customer).acount()
    return JsonResponse({"customer": customer.name, "count": count, "orders": orders})
```

| Task | Code |
| --- | --- |
| One object | `await Model.objects.aget(pk=1)`, `afirst()`, `alast()`, `alatest()`, `aearliest()` |
| Many objects | `async for obj in queryset:` or `[obj async for obj in queryset]` |
| Large result sets | `async for obj in queryset.aiterator(chunk_size=2000):` (server-side cursor) |
| Values | `async for row in queryset.values("id", "name"):` |
| Counting and checks | `acount()`, `aexists()`, `acontains(obj)`, `aaggregate(total=Sum("x"))` |
| Create | `await Model.objects.acreate(...)`, `abulk_create([...])`, `aget_or_create(...)`, `aupdate_or_create(...)` |
| Update | `await queryset.aupdate(field=F("field") + 1)`, `await obj.asave()`, `abulk_update(objs, ["field"])` |
| Delete | `await queryset.adelete()`, `await obj.adelete()` (cascades and signals run) |
| Reload | `await obj.arefresh_from_db()` |
| Related managers | `await order.items.acreate(...)`, `aadd()`, `aremove()`, `aset()`, `aclear()`, `acount()` |
| Raw SQL | `async for obj in Model.objects.raw("SELECT ...", [param]):` |

Query construction (`filter`, `annotate`, `select_related`, ...) is the same
synchronous, lazy API as always. Only evaluation needs `await`.

### Relations

Reading a relation that was not loaded triggers a query. In async code Django
forbids that implicit query, and so does asyncdjango:

```python
order = await Order.objects.aget(pk=1)
order.customer          # SynchronousOnlyOperation: load it explicitly

order = await Order.objects.select_related("customer").aget(pk=1)
order.customer.name     # fine

customers = [c async for c in Customer.objects.prefetch_related("orders")]
for customer in customers:
    for order in customer.orders.all():   # served from the prefetch cache
        ...
```

Inside code that asyncdjango runs natively (DRF serializer fields,
`sync_to_native` functions), lazy relation reads work as in sync Django and
each becomes an awaited query. Use `select_related`/`prefetch_related` there
too, to avoid N+1 queries.

### Generic relations

A `GenericRelation` manager looks up its `ContentType`. `NativeASGIHandler`
fills Django's content-type cache at startup. In scripts, call
`await asyncdjango.asgi.warm_content_types()` once first.

## 4. Transactions

```python
from django.db import transaction


async def transfer(request):
    async with transaction.atomic():
        source = await Account.objects.select_for_update().aget(pk=1)
        target = await Account.objects.select_for_update().aget(pk=2)
        source.balance -= 100
        target.balance += 100
        await source.asave(update_fields=["balance"])
        await target.asave(update_fields=["balance"])
        transaction.on_commit(lambda: notify.delay(source.pk, target.pk))
    return JsonResponse({"ok": True})
```

- Nesting creates savepoints; an exception inside a nested block rolls back
  only that block. `durable=True`, `transaction.set_rollback(True)`, and
  `transaction.get_rollback()` behave as in Django.
- `@transaction.atomic` works on `async def` functions.
- `ATOMIC_REQUESTS = True` works for async views with `NativeASGIHandler`.
- `asyncdjango.db.transaction.on_commit()` also accepts an `async def`
  callback, which is awaited after the commit.
- Inside the block, the task owns one connection. Concurrent child tasks
  (`asyncio.gather(Model.objects.aget(...), ...)`) cannot share it and raise
  `CrossTaskConnectionError`. Await ORM calls one after another inside a
  transaction. Outside a transaction, `gather` gives each call its own
  connection.
- If an async generator opens `atomic()`, close the generator
  (`async with contextlib.aclosing(gen) as rows:`). An abandoned one is rolled
  back when it is garbage-collected, with a warning.

## 5. Calling other async services

Any asyncio library works in async views and async hooks, for example
`redis.asyncio`, `httpx.AsyncClient`, `aiobotocore`, and `aiosmtplib`. They
run on the same event loop as the ORM, with no thread involved:

```python
import asyncio

from django.http import JsonResponse

from .clients import http_client, redis_client


async def dashboard(request, pk):
    # Independent I/O can run concurrently outside a transaction.
    customer, cached, rates = await asyncio.gather(
        Customer.objects.aget(pk=pk),
        redis_client().get(f"customer:{pk}:stats"),
        http_client().get("https://api.example.com/rates"),
    )
    return JsonResponse({
        "customer": customer.name,
        "stats": cached.decode() if cached else None,
        "rates": rates.json(),
    })
```

### Reuse one client per worker

Async clients keep connection pools tied to the event loop that created them.
Create them once per worker (per event loop), not once per request:

```python
# myapp/clients.py
import asyncio

import httpx
import redis.asyncio as redis

_http: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
_redis: dict[asyncio.AbstractEventLoop, redis.Redis] = {}


def http_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    if loop not in _http:
        _http[loop] = httpx.AsyncClient(timeout=10)
    return _http[loop]


def redis_client() -> redis.Redis:
    loop = asyncio.get_running_loop()
    if loop not in _redis:
        _redis[loop] = redis.Redis.from_url("redis://localhost:6379/0")
    return _redis[loop]
```

### Inside transactions

Awaiting an external service inside `async with transaction.atomic()` keeps
the transaction open, and its pooled connection busy, for the whole call. Call
slow services before the transaction, or after it with `on_commit`:

```python
from asyncdjango.db import transaction


async def place_order(request):
    quote = (await http_client().post(PRICING_URL, json=payload)).json()  # before
    async with transaction.atomic():
        order = await Order.objects.acreate(total=quote["total"])
        transaction.on_commit(lambda: send_confirmation(order.pk))     # async def is fine
    return JsonResponse({"id": order.pk})
```

### From synchronous hooks

Hooks that asyncdjango runs natively can be synchronous: DRF
authentication, permission and throttle classes, serializer fields,
`save()` overrides, and signal receivers. You have two options there:

- Write the hook as `async def`. asyncdjango awaits async `authenticate()`,
  `has_permission()`, `allow_request()`, `get_<field>` serializer methods, and
  signal receivers.
- Call `async_to_sync(client.get)(url)`. In native code this awaits on the
  current event loop instead of raising "cannot use AsyncToSync in the same
  thread as an async event loop".

### Do not

- Use blocking clients (`requests`, synchronous `redis.Redis`, `boto3`,
  `smtplib`) in async views or natively run hooks. They stall every request
  in the worker. Wrap them in `sync_to_async`, or use an async client.
- Rely on Django's cache API for non-blocking access: `await cache.aget(...)`
  with Django's Redis backend still uses a thread internally. Use
  `redis.asyncio` directly when it matters.

## 6. Synchronous code

| The function... | Call it with |
| --- | --- |
| only touches the database (queries, lazy relations, `save()`) | `await sync_to_native(fn)(...)` |
| performs other blocking I/O (HTTP, files, boto3, sync Redis) | `await sync_to_async(fn)(...)` |
| validates a DRF serializer | `await serializer.ais_valid()` |
| renders serializer data | `await serializer.adata` |

```python
from asgiref.sync import sync_to_async

from asyncdjango import sync_to_native


def monthly_report(customer):      # ORM only
    return {
        "orders": customer.orders.count(),
        "latest": customer.orders.latest("created").total,
    }


async def report_view(request, pk):
    customer = await Customer.objects.aget(pk=pk)
    report = await sync_to_native(monthly_report)(customer)
    pdf = await sync_to_async(render_pdf)(report)    # blocking library: thread
    return FileResponse(pdf)
```

`sync_to_native` runs on the event loop and joins the current transaction.
`sync_to_async` runs in a thread with Django's per-thread connection, so it
does not see an open native transaction. Keep that in mind when mixing them.

### Celery tasks and other synchronous callers

Synchronous code often reuses async services through `async_to_sync`:

```python
@shared_task
def verify_payments():
    async_to_sync(run_verification)()     # an async service also used by views
```

asgiref runs each such call on a new event loop that exists only for that
call, while the calling thread waits. Inside it, every ORM call (`aget`,
`asave`, `async with transaction.atomic()`, `sync_to_native`, ...) runs on the
calling thread with its Django connection, exactly as without asyncdjango. No
connection pool is created per call, Celery's connection cleanup after each
task still applies, and the code sees transactions open on that thread (as in
a test on Django's `TestCase`). The calling thread is idle while it waits, so
this adds no thread. Like `sync_to_async` in plain Django, concurrent calls
in that loop (`asyncio.gather`) take turns on the thread.

This applies to `async_to_sync` called from synchronous code: Celery tasks,
management commands, scripts, and Django's test cases. Inside an ASGI server,
`async_to_sync` reaches the server's loop and stays native. A long-running
program that starts its own loop with `asyncio.run()` also gets the native
path with a connection pool.

Set `ASYNCDJANGO["NATIVE_IN_ASYNC_TO_SYNC"] = True` to use the native path in
these loops too. Each call then opens a pool for the loop's lifetime (on
Windows, the loop must be a selector loop) and its queries do not see the
calling thread's transaction.

## 7. Middleware

### What runs where

| Middleware | Runs |
| --- | --- |
| Your async middleware (function or class, see below) | on the event loop |
| Django's built-in middleware (`SecurityMiddleware`, `CommonMiddleware`, `CsrfViewMiddleware`, `GZipMiddleware`, `XFrameOptionsMiddleware`, `ConditionalGetMiddleware`, `LocaleMiddleware`, `ContentSecurityPolicyMiddleware`, `CurrentSiteMiddleware`, flatpages, redirects, admindocs) | on the event loop |
| `SessionMiddleware`, `AuthenticationMiddleware`, `LoginRequiredMiddleware`, `MessageMiddleware` (and CSRF) | on the event loop when `SESSION_ENGINE` is the database or signed-cookie backend; otherwise in a thread |
| Cache middleware, `BrokenLinkEmailsMiddleware` | in a thread (they do blocking I/O) |
| Your `MiddlewareMixin` subclasses | in a thread, unless listed in `NATIVE_SYNC_MIDDLEWARE` |
| Sync-only middleware (`async_capable = False`, for example WhiteNoise) | in a thread, unless listed in `NATIVE_SYNC_MIDDLEWARE` |

Without asyncdjango, every `MiddlewareMixin`-based middleware, including all of
Django's own, costs a thread hop for `process_request` and another for
`process_response` on each ASGI request.

### Writing async middleware

Function style:

```python
from django.utils.decorators import async_only_middleware


@async_only_middleware
def tenant_middleware(get_response):
    async def middleware(request):
        request.tenant = await Tenant.objects.aget(slug=request.headers["X-Tenant"])
        response = await get_response(request)
        response["X-Tenant"] = request.tenant.slug
        return response

    return middleware
```

Class style, supporting both sync and async stacks:

```python
import uuid

from asgiref.sync import iscoroutinefunction, markcoroutinefunction


class RequestIDMiddleware:
    sync_capable = True    # both flags are required: without async_capable,
    async_capable = True   # Django treats the class as sync-only

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self.get_response):
            return self.__acall__(request)
        request.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response

    async def __acall__(self, request):
        request.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response = await self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response
```

Async middleware can await anything: the ORM, `redis.asyncio`, `httpx`. The
optional hooks can be `async def` too. An async `process_exception()` runs
natively; a synchronous one runs natively only if its class is on the native
list.

```python
from django.utils.deprecation import MiddlewareMixin


class ErrorAuditMiddleware(MiddlewareMixin):
    async def process_exception(self, request, exception):
        await AuditLog.objects.acreate(message=repr(exception))
        return None     # let Django build the error response
```

### Existing synchronous middleware

If a synchronous middleware (or a `MiddlewareMixin` subclass) does no blocking
I/O other than database access, list it to run natively:

```python
ASYNCDJANGO = {
    "NATIVE_SYNC_MIDDLEWARE": [
        "myapp.middleware.AuditMiddleware",
    ],
}
```

Matching is by exact class path. A subclass of a built-in middleware is not
trusted automatically. Do not list middleware that reads files (WhiteNoise),
calls HTTP services, or uses a synchronous cache.

In async views and async middleware, read the user with
`user = await request.auser()`: `request.user` is a lazy object whose first
access queries synchronously, which Django forbids in async code. (DRF views
from `asyncdjango.rest_framework` resolve `request.user` for you.)

`python manage.py check` lists what still runs in a thread:

- `asyncdjango.W002`: a sync-only middleware.
- `asyncdjango.W003`: a `MiddlewareMixin` middleware whose hooks will run in
  a thread.

## 8. REST APIs

`asyncdjango.rest_framework` provides adrf's API, so existing adrf code needs
only new imports (see [porting.md](porting.md)).

```python
from rest_framework import permissions
from rest_framework.response import Response

from asyncdjango.rest_framework import generics, serializers, viewsets
from asyncdjango.rest_framework.routers import DefaultRouter
from asyncdjango.rest_framework.views import APIView


class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    shipping = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "total", "customer", "customer_name", "shipping"]

    def get_customer_name(self, order):
        return order.customer.name                     # awaited query

    async def get_shipping(self, order):
        response = await http_client().get(f"{SHIPPING_URL}/{order.pk}")
        return response.json()["status"]


class IsOwner(permissions.BasePermission):
    async def has_object_permission(self, request, view, obj):
        return await obj.owners.filter(pk=request.user.pk).aexists()


class OrderViewSet(viewsets.ModelViewSet):          # alist, acreate, aretrieve, ...
    queryset = Order.objects.select_related("customer")
    serializer_class = OrderSerializer
    permission_classes = [IsOwner]

    async def perform_acreate(self, serializer):
        await serializer.asave(created_by=self.request.user)


class Summary(APIView):
    async def get(self, request):
        return Response({"orders": await Order.objects.acount()})


router = DefaultRouter()
router.register("orders", OrderViewSet)
```

- Serializers: `await serializer.adata`, `await serializer.ais_valid(raise_exception=True)`,
  `await serializer.asave(**extra)`. Override `acreate`/`aupdate` for custom
  writes; the defaults run DRF's `create()`/`update()` natively.
- Views: `await self.aget_object()`, `afilter_queryset()`,
  `apaginate_queryset()`, `get_apaginated_response()`.
- Authentication, permission, throttle, filter, and paginator classes may be
  synchronous (run natively) or `async def` (awaited).
- `@api_view(["GET"])` from `asyncdjango.rest_framework.decorators` accepts
  `async def` functions.
- Async viewsets call the `a`-prefixed hooks (`perform_acreate`, ...); a
  DRF-style `perform_create()` override is not called.

## 9. Signals

Receivers work as in Django:

```python
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=Order)
def log_order(sender, instance, created, **kwargs):      # sync: runs natively
    if created:
        AuditLog.objects.create(message=f"order {instance.pk}")


@receiver(post_save, sender=Order)
async def index_order(sender, instance, **kwargs):       # async: awaited
    await search_client().index(instance.pk)
```

During `asave()`/`adelete()`, receivers run in the saving task and join its
transaction. Receivers must not block on non-database I/O; send email and
webhooks from Celery, or from `transaction.on_commit`.

## 10. Testing

Django's `TestCase` wraps each test in a transaction on its synchronous
connection. Its async test methods (and synchronous tests that call async code
through `async_to_sync`) run their ORM calls on that connection, as described
in [Celery tasks and other synchronous callers](#celery-tasks-and-other-synchronous-callers),
so they pass but do not exercise the native path. Native queries cannot see
that transaction. To test the native path, use asyncdjango's test cases, and
run them with Django's test runner as usual:

```python
from asyncdjango.test import ASGIClient, NativeTestCase, NativeTransactionTestCase
from asyncdjango.asgi import NativeASGIHandler


class OrderTests(NativeTestCase):          # rolled back after each test
    async def test_create(self):
        order = await Order.objects.acreate(total=10)
        self.assertEqual(await Order.objects.acount(), 1)

    async def test_api(self):
        client = ASGIClient(NativeASGIHandler())
        response = await client.post("/api/orders/", {"total": "10.00"})
        self.assertEqual(response.status_code, 201)


class ConcurrencyTests(NativeTransactionTestCase):   # real commits, then flush
    async def test_parallel_requests(self):
        ...
```

Useful diagnostics:

```python
from asyncdjango.diagnostics import EventLoopBlockDetector, ThreadOffloadDetector


async def test_no_blocking(self):
    async with EventLoopBlockDetector(threshold=0.05):      # fails on a stalled loop
        await client.get("/api/orders/")

async def test_no_threads(self):
    async with ThreadOffloadDetector():                     # fails on any thread hop
        await client.get("/api/orders/")
```

## 11. Settings reference

```python
ASYNCDJANGO = {
    "PATCH_ORM": True,                 # native async ORM for all models
    "ALLOW_SYNC_FALLBACK": False,      # non-PostgreSQL aliases: raise (False) or thread + warning (True)
    "NATIVE_IN_ASYNC_TO_SYNC": False,  # async_to_sync() from sync code (Celery): caller's thread (False) or native (True)
    "POOL": {"min_size": 1, "max_size": 10, "timeout": 30.0},
    "POOLS": {},                       # per-alias overrides, e.g. {"replica": {"max_size": 4}}
    "NATIVE_SYNC_MIDDLEWARE": [],      # extra middleware classes to run natively
    "NATIVE_BUILTIN_MIDDLEWARE": True, # run Django's non-blocking built-in middleware natively
}
```

Pool sizing, servers, security, and observability are covered in
[deployment.md](deployment.md).

## 12. Troubleshooting

| Error or symptom | Cause and fix |
| --- | --- |
| `SynchronousOnlyOperation` / `MissingNativeContext` | A synchronous ORM call (or a lazy relation read) in async code. Use the `a`-method, `select_related`/`prefetch_related`, or `sync_to_native`. |
| `CrossTaskConnectionError` | Concurrent tasks inside `transaction.atomic()`. Await sequentially inside the block, or move the concurrency outside it. |
| `NativeAsyncUnsupported` | The query targets a non-PostgreSQL (or psycopg2) alias. Route it to PostgreSQL, or enable `ALLOW_SYNC_FALLBACK`. |
| `OperationalError: couldn't get a connection after 30.00 sec` | Pool exhausted: too many concurrent requests holding connections, or long transactions. Raise `POOL.max_size` (within PostgreSQL's `max_connections`) or shorten transactions. |
| `UnsupportedEventLoop` or `Psycopg cannot use the 'ProactorEventLoop'` | Windows default event loop. Use a selector event loop, WSL, or Docker. (`async_to_sync` from synchronous code is not affected unless `NATIVE_IN_ASYNC_TO_SYNC` is on.) |
| Lifespan not supported / pools open only on the first request under Channels | `ProtocolTypeRouter` has no `"lifespan"` entry. Map it to the same handler as `"http"` ([§2](#asgi-entry-point)). |
| Requests stall under load | Something blocks the loop: a sync client in a hook, heavy CPU work, or a large synchronous render. Find it with `EventLoopBlockDetector`; use async clients, `sync_to_async`, or more workers. |
| `manage.py check` W002/W003 | Middleware running in a thread. See [§7](#7-middleware). |
| Native queries missing from `connection.queries` / debug toolbar | They are logged per native connection. Use an execute wrapper from `connection_created` for metrics. |
