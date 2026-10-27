# TODO

The project was re-based on 2026-10-15 from a standalone framework (own
model layer, router, and migrations on asyncpg; preserved on `main`) to a
Django app on real Django models. See README.md for the architecture.

## Project rules

- [x] Keep Django and DRF/ADRF syntax; reuse Django's own ORM code instead of reimplementing it.
- [x] Keep the request and ORM path free of `sync_to_async`, `asyncio.to_thread`, and executors, guarded by a source test and a runtime `ThreadOffloadDetector` in the suite.
- [x] Accidental synchronous ORM use from async code still raises `SynchronousOnlyOperation`.
- [x] Pin the Django/DRF versions whose internals are patched or adapted; record every patch and adaptation with licenses (`docs/upstream.md`, `licenses/`).
- [x] Prove behavior with live PostgreSQL integration tests.
- [ ] Choose a license for asyncdjango itself before publishing.

## 0. Native PostgreSQL execution

- [x] Greenlet bridge: Django's synchronous ORM on the event loop, I/O awaited (`db/bridge.py`).
- [x] psycopg 3 `AsyncConnectionPool` per event loop and alias; time zone, role, and isolation level configured per physical connection.
- [x] Native `DatabaseWrapper` (Django's own, with an async connection facade), including server-side cursors.
- [x] Task-owned scopes; `connections[alias]` routed to the active scope; cross-task use of a transaction rejected.
- [x] ASGI lifespan opens and closes pools; unclosed pools no longer hang `asyncio.run()` shutdown.
- [x] Loop shutdown closes every connection a pool opened (psycopg_pool's `close()` aborts on its cancelled workers and left idle connections open: one leak per `asyncio.run()`).
- [x] `async_to_sync()` from synchronous code (Celery tasks, scripts, Django's `TestCase`) runs ORM calls on the caller's thread and connection instead of a pool per call; `NATIVE_IN_ASYNC_TO_SYNC` opts back in.
- [x] Cancellation (client disconnect, task cancel) rolls back and returns the connection.
- [ ] Verify the thread-free query cancel path with libpq 17+ (psycopg-binary 3.3.2 bundles libpq 16.9, which cancels from a thread).
- [ ] Reuse `DatabaseWrapper` objects across scopes if profiling on Linux shows the per-call construction matters.

## 1. Django ORM API

- [x] All `QuerySet` async methods, async iteration (models, values, values_list, raw), `aiterator` with server-side cursors and per-chunk prefetch.
- [x] `Model.asave()`, `adelete()`, `arefresh_from_db()` with signals, cascades, and `save()` overrides.
- [x] Related managers: reverse FK, many-to-many, generic relations.
- [x] `async with transaction.atomic()` (Django's and asyncdjango's), savepoints, durable, `set_rollback`, sync and async `on_commit`, `select_for_update`.
- [x] Async signal receivers and `async_to_sync` inside native code.
- [x] Opt-in `NativeManager`/`NativeModelMixin` for `PATCH_ORM = False`.
- [x] Non-PostgreSQL aliases raise `NativeAsyncUnsupported`, or fall back with `SyncFallbackWarning`.
- [x] ContentType cache warmed at startup for generic relations.
- [ ] Native `SessionStore` async methods (db backend `asave` uses a thread) and remaining contrib async helpers.
- [ ] `COPY` through the facade (currently raises; use psycopg directly).

## 2. ASGI and REST

- [x] `NativeASGIHandler`: request signals, rendering, exception handling, logging, and `response.close()` without threads; thread-bound requests still close their thread's connections.
- [x] `ATOMIC_REQUESTS` for async views; opt-in native sync middleware; system checks for sync-only middleware and non-native aliases.
- [x] Django's non-blocking built-in middleware runs natively (MiddlewareMixin hooks); async `process_exception` hooks run natively; check W003 for thread-bound MiddlewareMixin middleware.
- [x] User guide (`docs/guide.md`), including async service clients and middleware.
- [x] `asyncdjango.rest_framework` with ADRF's API: APIView, generics, mixins, viewsets, routers, serializers (async method fields, nested, many), `@api_view`; sync and async auth/permission/throttle/filter/paginator hooks.
- [x] Test helpers: `NativeTestCase` (rollback), `NativeTransactionTestCase` (flush), in-process `ASGIClient`.
- [x] Django Channels: document and test routing the `http` and `lifespan` scopes to `NativeASGIHandler`; websocket consumers and middleware on the native ORM.

## 3. Compatibility and release

- [x] Compare correctness and concurrency against Django/ADRF under the same PostgreSQL workload (`benchmarks/`, `docs/benchmarks.md`; identical response fingerprints).
- [x] Document configuration, migrations, deployment, security, and supported versions (`docs/deployment.md`, `docs/compatibility.md`, `docs/porting.md`).
- [ ] Port a slice of the CRM on a branch of the CRM repository (imports, `sync_to_async` → `ais_valid`/`adata`/`sync_to_native`, `async_capable` on its two logging middlewares) and run its test suite.
- [x] GitHub Actions workflow: suite on Python 3.12-3.14 x PostgreSQL 14/18 service containers; optional benchmark job.
- [ ] Confirm the first Linux CI run is green (verified locally on Windows with Python 3.12 and 3.13 only).
- [ ] Benchmark with a real server (uvicorn + an HTTP load generator) on Linux.
- [ ] Decide whether to support Django 5.2 LTS (untested; the patched internals are similar).
- [ ] Define release criteria and publish only after the CRM slice runs in staging.
