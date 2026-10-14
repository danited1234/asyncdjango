# asyncdjango

An experimental async-native framework aiming for Django and Django REST Framework-style application code. This repository is at the foundation stage; it is **not** a Django replacement yet.

The central rule is that request-time I/O must await an async driver directly. The normal database path must not use `sync_to_async`, `asyncio.to_thread`, or an executor. CPU-only work such as building a query may stay synchronous.

## Current scope

The first code is a PostgreSQL foundation built on `asyncpg`. It has an async connection pool, transactions, Django-style model declarations, lazy SQL construction, and an initial native async CRUD API. Initial table migrations, a JSON ASGI router, serializers, and model viewsets are implemented. The broader Django/DRF feature set is still in progress.

The ASGI `DatabaseLifespan` wrapper opens and closes the pool at application startup and shutdown. A transaction yields a task-owned connection; use its methods inside that transaction. Nested transaction blocks create PostgreSQL savepoints. `set_rollback()` rolls back the current transaction level when its block exits.

Foreign keys support multi-level joins such as
`select_related("department__company")`. Reading an unloaded forward relation
raises `UnloadedRelationError` so it cannot trigger an implicit database query.
Reverse foreign keys expose a lazy queryset, for example
`await organization.employee_set.fetch_all()`. Many-to-many declarations use
`models.ManyToManyField(Tag, related_name="articles")`; relation managers
provide awaited `add`, `remove`, `clear`, `set`, `fetch_all`, and `count`.
Accessing either relation manager performs no I/O. Initial migrations create
the many-to-many join table after its target tables.
The initial serializer checks field types synchronously and checks unique and foreign-key values with awaited database queries. Database unique constraints still enforce correctness if concurrent requests race.
The test suite scans the source for thread-offload calls and includes `EventLoopBlockDetector` to flag a stalled event loop during focused development tests.

Initial and column-alteration migrations are documented in
[`docs/migrations.md`](docs/migrations.md). Review generated SQL before applying
destructive changes.

## Try the database spike

Use Python 3.11+ and a PostgreSQL database you can connect to:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
$env:ASYNCDJANGO_DATABASE_URL = "postgresql://user:password@localhost:5432/database"
.venv\Scripts\python examples\native_async.py
```

The example runs five independent `pg_sleep(0.1)` queries concurrently. It reports elapsed time but makes no fixed performance assertion because database and machine conditions vary.

The current model API binds a pool or transaction explicitly:

```python
from asyncdjango.db import AsyncPostgres, models

class User(models.Model):
    email = models.CharField(max_length=255)

database = AsyncPostgres("postgresql://user:password@localhost/database")
await database.connect()
try:
    user = await User.objects.using(database).get(pk=1)
    async for other in User.objects.using(database).filter(email=user.email):
        print(other.pk)
finally:
    await database.close()
```

Normal query construction is lazy; `get`, `fetch_all`, writes, and async iteration perform I/O. A transaction connection can be passed to `.using(transaction)` to keep its queries inside the transaction. An initial migration can create the table with `initial_migration("0001_initial", [User])` and `await apply_migration(database, migration)`. Later schema changes are not generated yet.

## API direction

Query construction should preserve familiar Django expressions where practical:

```python
queryset = User.objects.filter(is_active=True).order_by("-created_at")
user = await User.objects.get(pk=1)

async with transaction.atomic():
    user = await User.objects.select_for_update().get(pk=1)
    user.balance -= 10
    await user.save()
```

The code above is a target API, **not implemented yet**. A database query, lazy relation load, or write must have an explicit `await` boundary. See [todo.md](todo.md) for implementation order and acceptance criteria.

The intended Django/DRF syntax and known incompatibilities are in [docs/compatibility.md](docs/compatibility.md).

Run tests after the editable install with `.venv\Scripts\python -m unittest discover -s tests`. If `ASYNCDJANGO_DATABASE_URL` points to a disposable PostgreSQL database, the same command also runs integration tests. Those tests create and drop a table named `asyncdjango_test_users` in that database.

## Compatibility policy

Aim to keep Django and DRF application syntax for models, query expressions, serializers, routers, and viewsets. Document every incompatible behavior rather than silently falling back to a thread. Start with PostgreSQL; add other databases only after the PostgreSQL execution path is complete.

## Streaming HTTP bodies

For an upload endpoint that should process chunks before the entire body has
arrived, register the route with `stream_request=True` and iterate over
`request.stream()` in the async handler. The stream is single-use. The default
route mode continues to buffer up to `max_body_size` bytes and exposes
`request.body` as bytes. Streaming routes enforce the same limit while data
arrives; `request.body` is `None` until `await request.json()` consumes it.

`StreamingResponse(async_iterable)` writes each bytes chunk directly to ASGI
`send`. Disconnects cancel the active handler or response stream. Async
middleware registered with `add_middleware()` wraps matching handlers in
registration order, with the first middleware outermost.

## Schema and in-process testing

`generate_openapi(app)` produces an OpenAPI 3.1 document for registered routes.
Routes registered through `Router` include model serializer field schemas and
request/response shapes; ad hoc routes receive basic path and response entries.
`AsyncTestClient(app)` drives ASGI lifespan startup/shutdown and lets tests use
`await client.get(...)`, `await client.post(..., json_data=...)`, or
`await client.request(..., body=async_chunks)` without an HTTP server.

## REST access policies and query controls

`APIView` and `ModelViewSet` accept DRF-style `authentication_classes`,
`permission_classes`, and `throttle_classes`. Authentication and object
permission hooks are awaited directly. `TokenAuthentication` parses the
`Authorization: Token ...` header; applications subclass its
`authenticate_credentials()` with their own native async credential lookup.
No default token store is bundled. Keep tokens out of logs and require HTTPS.

`SimpleRateThrottle` enforces an exact sliding window within one process.
It is **not** a distributed or durable rate limiter; production deployments
with multiple workers need a shared atomic backend before relying on it for
abuse protection. `ExactFilterBackend` only accepts named `filterset_fields`,
`OrderingFilter` only accepts named `ordering_fields`, and
`PageNumberPagination` uses awaited count and sliced PostgreSQL queries.

## Serializer hooks and nested writes

`ModelSerializer` supports `to_internal_value()`, `to_representation()`,
`validate_<field>()`, and `validate()` hooks. Field and object validators may
be synchronous pure-Python functions or `async def`; call `await ais_valid()`
to run async hooks and database-backed checks. Declare a nested foreign-key
serializer as a class attribute, such as `organization = OrganizationSerializer()`.
Nested input is validated and represented when the relation was explicitly
loaded, but, as in DRF, writes require explicit `async def create()` and
`async def update()` methods on the parent serializer. `await asave()` wraps
the operation in a native async transaction so a failed nested write rolls
back all rows. The default methods reject nested writes rather than guessing
whether to create, replace, clear, or delete related data.
