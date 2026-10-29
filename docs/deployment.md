# Configuration and deployment

## Supported versions

Python 3.12+ (Django 6.0's minimum, and asyncdjango's on 5.2 too), Django
6.0.x or 5.2.x LTS, Django REST framework 3.16.x (optional), psycopg 3.2+ with
psycopg-pool 3.2+, greenlet 3.0+, and PostgreSQL 14+ (the minimum of both
Django releases). The verified combination is listed in
[upstream.md](upstream.md). Linux and macOS work as is. Windows works for
development with a selector event loop (see [compatibility.md](compatibility.md)).

## Settings

```python
ASYNCDJANGO = {
    # Replace Django's thread-based async ORM methods with native ones.
    "PATCH_ORM": True,
    # Raise NativeAsyncUnsupported (False) or fall back to Django's thread
    # implementation with a SyncFallbackWarning (True) for non-native aliases.
    "ALLOW_SYNC_FALLBACK": False,
    # async_to_sync() called from synchronous code (Celery tasks, scripts):
    # run ORM calls on the calling thread's Django connection (False) or
    # natively with a pool for that call's event loop (True).
    "NATIVE_IN_ASYNC_TO_SYNC": False,
    # psycopg_pool.AsyncConnectionPool options for every alias.
    "POOL": {"min_size": 1, "max_size": 10, "timeout": 30.0},
    # Per-alias overrides merged over POOL.
    "POOLS": {"replica": {"max_size": 4}},
    # Extra middleware classes whose sync hooks run on the event loop.
    "NATIVE_SYNC_MIDDLEWARE": [],
    # Run Django's built-in middleware that does no blocking I/O natively.
    "NATIVE_BUILTIN_MIDDLEWARE": True,
}
```

`POOL` accepts any `AsyncConnectionPool` keyword: `min_size`, `max_size`,
`timeout` (seconds to wait for a free connection before
`django.db.OperationalError`), `max_waiting`, `max_lifetime`, `max_idle`,
`reconnect_timeout`, `num_workers`.

The usual `DATABASES` options apply to native connections: `NAME`, `USER`,
`PASSWORD`, `HOST`, `PORT`, `TIME_ZONE`, `OPTIONS` (including
`isolation_level`, `assume_role`, `server_side_binding`, `sslmode` and other
libpq parameters), `AUTOCOMMIT`, `ATOMIC_REQUESTS` (supported for async views
by `NativeASGIHandler`), `DISABLE_SERVER_SIDE_CURSORS`, and
`CONN_HEALTH_CHECKS` (enables the pool's connection check). `CONN_MAX_AGE` and
`OPTIONS["pool"]` configure Django's synchronous connections only.

Prepared statements stay disabled (Django's default), so PgBouncer in
transaction mode works.

## Connection budget

Each worker process has its own pools: at most `POOL.max_size` connections
per alias. A request uses one connection per concurrently running ORM call,
and a request inside `async with transaction.atomic()` holds one for the
whole block. Budget:

```
web workers × max_size  +  Celery concurrency  +  management commands  +  headroom
    <=  PostgreSQL max_connections   (or the PgBouncer pool size)
```

Celery workers keep Django's one connection per worker thread: async
services they call through `async_to_sync` use it rather than a pool (see
the [guide](guide.md#celery-tasks-and-other-synchronous-callers)).

### One connection per request

By default every awaited ORM call checks a connection out of the pool and
returns it right away, so a request that spends most of its time awaiting
other services holds no connection meanwhile. With

```python
ASYNCDJANGO = {"CONNECTION_PER_REQUEST": True}
```

the ORM calls of a request share one connection, checked out by the first
and returned when the response is ready, as a thread's connection is in sync
Django. Each call then skips the checkout and the setup of its own Django
connection object, which saves CPU on endpoints that make several queries,
and `SESSION_SQL` runs at most once per request. The price is that a request
holds its connection while it awaits anything else (an HTTP API, S3), so size
the pool for the requests in flight rather than for the queries in flight.
Child tasks (`asyncdjango.gather()`, `asyncio.create_task()`) still get
connections of their own, and a transaction still refuses them.

When the pool is exhausted, requests wait up to `timeout` seconds and then
fail with `OperationalError`. That is back-pressure, not a leak. Watch
`asyncdjango.db.pool_stats()` (from inside the event loop) to tune it.

## Server

Use an ASGI server with lifespan support, and point it at
`asyncdjango.asgi.get_asgi_application()`:

```console
gunicorn project.asgi:application -k uvicorn.workers.UvicornWorker --workers 4
uvicorn project.asgi:application --workers 4 --lifespan on
```

At startup the handler opens the pools (without waiting for connections) and
loads content types into Django's cache. At shutdown it closes the pools. If
a server sends no lifespan events, pools open on first use. A loop that ends
without closing its pools still closes them, and every connection they
opened, while `asyncio.run()` shuts down.

With Django Channels, map both `"http"` and `"lifespan"` in
`ProtocolTypeRouter` to the handler ([guide](guide.md#asgi-entry-point));
the router rejects scope types it has no entry for.

One worker process handles many concurrent requests on one core. Run about
one worker per core, as with any asyncio server. CPU-heavy serialization
scales with workers, not with concurrency inside a worker.

Scripts and management commands can use the native ORM inside
`asyncio.run(...)`, which creates a pool for that loop. Call `await
asyncdjango.db.close_pools()` at the end to avoid shutdown warnings. Code
they run through `async_to_sync(...)` uses the command's own Django
connection instead.

## Security

- asyncdjango does not change how queries are built: Django's compiler emits
  parameterized SQL, and psycopg binds parameters client-side as in sync
  Django. Raw SQL with `extra()`/`raw()` has the same injection rules as
  Django.
- Connection settings come from `DATABASES`; use `OPTIONS` for TLS
  (`sslmode="verify-full"`, `sslrootcert=...`). Credentials are not logged.
- `SET ROLE` (`OPTIONS["assume_role"]`) and other session state persist on
  pooled connections. Set per-request state such as a tenant's `search_path`
  through `ASYNCDJANGO["SESSION_SQL"]` (see
  [guide.md](guide.md#per-request-session-state)), which re-applies it
  whenever a connection's state differs, including after a rollback.
- The pool limit is also a protection: a flood of slow requests queues for
  connections instead of exhausting PostgreSQL. Pair it with server-side
  `statement_timeout` and an HTTP-level timeout.
- A cancelled request (client disconnect) rolls back its transaction, and
  psycopg cancels the running statement.

## Migrations

Unchanged: `makemigrations` and `migrate` are Django's synchronous commands
and do not use the native path. Run them as before.

## Observability

### Event-loop stalls

Every request in a worker shares one event loop, so a blocking call (a
synchronous HTTP client, password hashing, a large serializer, a slow import)
delays every other request on that worker. Set a threshold to find them:

```python
ASYNCDJANGO = {"STALL_THRESHOLD": 0.1}  # seconds
```

`NativeASGIHandler` then starts a watchdog for its loop. Each stall of at
least 0.1 s logs one warning on the `asyncdjango.stalls` logger, with its
length, the request, and the stack of the code that was blocking:

```
Event loop blocked for 0.312s during GET /api/invoices/; stack while blocked:
  ...
  File "invoices/views.py", line 41, in get
    pdf = render_pdf(invoice)
  ...
```

The stack is taken once, when the stall crosses the threshold, so a stall
made of several blocking calls shows the first. A stall shorter than about a
quarter of the threshold past it may end before the stack is taken; the
warning then says so. `asyncdjango.monitoring.stall_stats()` returns the
running loop's counters (`stalls`, `longest`, `total` seconds) for metrics.

The watchdog is one daemon thread per worker that only reads the heartbeat
and the loop thread's stack; the loop itself runs a timer every quarter of the
threshold. Both cost microseconds, so leaving it on in production is fine.
Route `asyncdjango.stalls` to your error tracker to see stalls in staging.

### Health checks and metrics

```python
from asyncdjango.views import health, metrics

urlpatterns = [
    path("internal/health/", health),
    path("internal/metrics/", metrics),
]
```

`health` checks out a connection from each native alias's pool and runs
`SELECT 1`. It answers 200 with the pool statistics and stall counters, or
503 when a database is unreachable or every connection stays busy for a
second (`path(..., health, {"timeout": 2.0})` changes that). Point the load
balancer's readiness probe at it: a worker that has exhausted its pool stops
receiving traffic.

`metrics` serves the same numbers in Prometheus' text format:
`asyncdjango_pool_size`, `asyncdjango_pool_available`,
`asyncdjango_pool_requests_waiting` (gauges), `asyncdjango_pool_requests_num_total`,
`asyncdjango_pool_requests_wait_ms_total`, `asyncdjango_pool_requests_errors_total`
and psycopg_pool's other counters, and `asyncdjango_loop_stalls_total` when the
stall watchdog is on. Every sample has a `worker` label (the process id).

Both describe the worker process that served the request: with several
workers, a scrape through the load balancer sees one at random. Scrape each
worker directly, or treat the numbers as samples. Keep both endpoints off the
public internet.

### Metrics and tracing

- `asyncdjango.db.pool_stats()` returns psycopg pool counters per alias.
- `connection.execute_wrapper()` installed from a `connection_created`
  receiver sees every native query (for metrics or tracing).
- `asyncdjango.diagnostics.EventLoopBlockDetector` and
  `ThreadOffloadDetector` help find blocking calls and thread hops in tests.
