# asyncdjango

A Django app that makes Django's async ORM and ADRF-style REST views truly
asynchronous on PostgreSQL. Add it to `INSTALLED_APPS` and existing code such
as `await Order.objects.aget(pk=1)`, `await order.asave()`,
`async for row in queryset`, and `async with transaction.atomic():` stops
using worker threads. Queries are awaited on the event loop through psycopg 3's
async driver and a connection pool.

Your models, managers, querysets, migrations, admin, and settings remain
Django's own. Nothing about the model layer is reimplemented.

> Status: 0.1.0a1, alpha. Verified with Python 3.13, Django 6.0.3,
> DRF 3.16.1, psycopg 3.3, PostgreSQL 18. See [docs/upstream.md](docs/upstream.md).

## Why

Django's async ORM methods are `await sync_to_async(self.get)(...)`: every
call hops to a thread. ADRF adds more hops (authentication, permissions,
throttles, and one per serializer field per row). Under load that costs CPU,
needs a thread per in-flight request, and makes Django's thread-local
connections hard to manage (`CONN_MAX_AGE = 0` to avoid orphaned connections,
so a new connection per request).

## Benchmarks

[`benchmarks/`](benchmarks/) runs the same endpoints on five stacks against
the same PostgreSQL data and checks that every stack returns identical
responses: asyncdjango with its DRF-style API or with plain Django views,
ADRF, synchronous DRF on a threaded WSGI server, and Django's own async views.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/bench-throughput-dark.svg">
  <img alt="Requests per second at 50 concurrent clients for each stack and scenario" src="docs/images/bench-throughput-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/bench-cpu-dark.svg">
  <img alt="CPU time per request at 50 concurrent clients for each stack and scenario" src="docs/images/bench-cpu-light.svg">
</picture>

At 50 concurrent clients on a 4-core laptop:

- **Compared with ADRF**, the same view and serializer code served about 10
  times the requests on the read endpoint (at a twelfth of the CPU per
  request) and twice as many on the write endpoint.
- **Compared with Django's async views**, plain Django views served about 30%
  more requests at 35–40% less CPU per request.
- **Compared with synchronous DRF**, throughput was about the same, on one
  thread instead of one per in-flight request (56 threads).
- **For a request that waits 50 ms on the database**, every stack is limited
  by the 20-connection pool (270–306 req/s).

Results vary between runs and machines. See
[docs/benchmarks.md](docs/benchmarks.md) for the method, all results with
their spread, and how to run the benchmark on your own hardware.

## How it works

asyncdjango runs Django's own synchronous ORM code in a
[greenlet](https://greenlet.readthedocs.io/) on the event loop thread, the
technique SQLAlchemy's asyncio extension uses. Its PostgreSQL wrapper is
Django's stock `DatabaseWrapper` whose raw connection is a facade over a pooled
`psycopg.AsyncConnection`. When the ORM reaches I/O, the facade switches back
to the event loop, awaits the query, and resumes the ORM code with the result.
Other requests run while the query is in flight, and no thread is used.

Because Django's code runs unchanged, its semantics carry over: signals,
`save()` overrides, delete cascades, `on_delete` handlers, savepoints,
`select_for_update`, `prefetch_related`, custom managers, execute wrappers,
and `connection_created` receivers.

Connections are owned by a *scope*. Outside a transaction, each awaited ORM
call borrows a pooled connection and returns it. Inside
`async with transaction.atomic()` the task keeps one connection until the
outermost block exits.

## Install

From GitHub (asyncdjango is not on PyPI yet):

```console
pip install "asyncdjango[drf] @ git+https://github.com/danited1234/asyncdjango.git@main"
pip install "psycopg[binary]"   # psycopg 3 with its own libpq
```

In `requirements.txt`, pin a commit so builds are reproducible:

```text
asyncdjango[drf] @ git+https://github.com/danited1234/asyncdjango.git@<commit-sha>
```

Drop `[drf]` if you don't use Django REST framework. The machine (or Docker
image) running pip needs `git`. While the repository is private, pip also
needs credentials, for example
`git+https://<token>@github.com/danited1234/asyncdjango.git@<commit-sha>` with a
fine-grained token that can read this repository (keep the token out of the
committed file; pass it as a build secret), or
`git+ssh://git@github.com/danited1234/asyncdjango.git@<commit-sha>` with a
deploy key.

From a local checkout, for development:

```console
pip install -e ".[drf]"
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "asyncdjango",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",  # psycopg 3
        # NAME, USER, PASSWORD, HOST, PORT as usual
    }
}

ASYNCDJANGO = {
    "POOL": {"min_size": 2, "max_size": 20},  # per worker process
}
```

```python
# asgi.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

from asyncdjango.asgi import get_asgi_application

application = get_asgi_application()
```

Run any ASGI server with lifespan support, for example
`gunicorn project.asgi:application -k uvicorn.workers.UvicornWorker`. The
handler opens the pools at startup and closes them at shutdown.

## Use

The ORM API is Django's:

```python
from django.db import transaction
from django.http import JsonResponse

async def transfer(request, source_id, target_id):
    async with transaction.atomic():
        source = await Account.objects.select_for_update().aget(pk=source_id)
        target = await Account.objects.select_for_update().aget(pk=target_id)
        source.balance -= 10
        target.balance += 10
        await source.asave()
        await target.asave()
    return JsonResponse({"ok": True})
```

REST views use ADRF's API from `asyncdjango.rest_framework`:

```python
from rest_framework.response import Response

from asyncdjango.rest_framework import serializers, viewsets
from asyncdjango.rest_framework.views import APIView

class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "total", "customer_name"]

    def get_customer_name(self, order):
        return order.customer.name  # an awaited query, not a thread hop

class OrderDetail(APIView):
    async def get(self, request, pk):
        order = await Order.objects.aget(pk=pk)
        return Response(await OrderSerializer(order).adata)

class OrderViewSet(viewsets.ModelViewSet):  # alist, acreate, aretrieve, ...
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
```

For synchronous helper code that only touches the database, replace
`sync_to_async` with `sync_to_native`:

```python
from asyncdjango import sync_to_native

is_valid = await sync_to_native(serializer.is_valid)()
```

`sync_to_native` runs on the event loop. Keep `sync_to_async` for code that
calls blocking libraries (HTTP clients, boto3, synchronous Redis).

## Read before adopting

- Synchronous hooks reached from native code (model `save()` overrides,
  signal receivers, DRF auth/permission/throttle classes, serializer fields
  and validators) run **on the event loop**. Database access in them is fine;
  other blocking I/O stalls the worker. Find it with
  `asyncdjango.diagnostics.EventLoopBlockDetector`.
- Child tasks cannot share an open transaction (`CrossTaskConnectionError`).
- Only PostgreSQL with psycopg 3 is native. Other aliases raise
  `NativeAsyncUnsupported` unless `ALLOW_SYNC_FALLBACK` is enabled.
- Async tests of native code must use `asyncdjango.test.NativeTestCase` or
  `NativeTransactionTestCase`. On Django's `TestCase`, async code runs its ORM
  calls on the test's connection instead, like Celery tasks that call async
  services through `async_to_sync` (no pool per task).
- With Django Channels, route both `"http"` and `"lifespan"` in
  `ProtocolTypeRouter` to the handler.

The complete list is in [docs/compatibility.md](docs/compatibility.md).

## Documentation

- [docs/guide.md](docs/guide.md): the user guide (setup, ORM, transactions,
  calling Redis/HTTP services, middleware, REST, signals, testing,
  troubleshooting).
- [docs/compatibility.md](docs/compatibility.md): what is native, what
  differs, what is unsupported.
- [docs/porting.md](docs/porting.md): moving an ADRF code base over.
- [docs/deployment.md](docs/deployment.md): settings, pool sizing, servers,
  security, supported versions.
- [docs/benchmarks.md](docs/benchmarks.md): method and results.
- [docs/upstream.md](docs/upstream.md): patched Django entry points, adapted
  upstream code, and licenses.
- [docs/releasing.md](docs/releasing.md): tagging releases and publishing to
  PyPI.

## License

BSD 3-Clause; see [LICENSE](LICENSE). Code adapted from Django and DRF keeps
its upstream notices in [licenses/](licenses/).

## Development

```console
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[drf,test]"   # .venv/bin/python on Linux/macOS
$env:ASYNCDJANGO_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/asyncdjango"
.venv/Scripts/python -m django test tests --settings=tests.settings
```

The test runner creates and drops `test_<database name>`.

GitHub Actions (`.github/workflows/tests.yml`) runs the suite on every push
and pull request against live PostgreSQL 14 and 18 service containers, on
Python 3.12, 3.13, and 3.14. Run the workflow manually with "benchmark"
checked to also run the five-stack benchmark; the table appears in the job
summary. The suite checks
that no request or ORM path offloads to a thread (`ThreadOffloadDetector`)
and that every Django async method that would use a thread is patched.
