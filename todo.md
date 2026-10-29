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
- [x] License asyncdjango under BSD-3-Clause, matching Django and DRF (`LICENSE`).

## 0. Native PostgreSQL execution

- [x] Greenlet bridge: Django's synchronous ORM on the event loop, I/O awaited (`db/bridge.py`).
- [x] psycopg 3 `AsyncConnectionPool` per event loop and alias; time zone, role, and isolation level configured per physical connection.
- [x] Native `DatabaseWrapper` (Django's own, with an async connection facade), including server-side cursors.
- [x] Task-owned scopes; `connections[alias]` routed to the active scope; cross-task use of a transaction rejected.
- [x] ASGI lifespan opens and closes pools; unclosed pools no longer hang `asyncio.run()` shutdown.
- [x] Loop shutdown closes every connection a pool opened (psycopg_pool's `close()` aborts on its cancelled workers and left idle connections open: one leak per `asyncio.run()`).
- [x] `async_to_sync()` from synchronous code (Celery tasks, scripts, Django's `TestCase`) runs ORM calls on the caller's thread and connection instead of a pool per call; `NATIVE_IN_ASYNC_TO_SYNC` opts back in.
- [x] Cancellation (client disconnect, task cancel) rolls back and returns the connection.
- [x] Thread-free query cancel with libpq 17+: psycopg-binary 3.3.6 bundles libpq 18.4, and the cancellation tests forbid a thread whenever libpq >= 17.
- [x] Measured `DatabaseWrapper` construction per scope: 5 µs (scope, wrapper, and release together 18 µs) against 745 µs for a native `SELECT 1` round trip; a request makes 2-3 ORM calls on a few ms of CPU, so reuse would save under 1%. Not worth the shared state.

## 1. Django ORM API

- [x] All `QuerySet` async methods, async iteration (models, values, values_list, raw), `aiterator` with server-side cursors and per-chunk prefetch.
- [x] `Model.asave()`, `adelete()`, `arefresh_from_db()` with signals, cascades, and `save()` overrides.
- [x] Related managers: reverse FK, many-to-many, generic relations.
- [x] `async with transaction.atomic()` (Django's and asyncdjango's), savepoints, durable, `set_rollback`, sync and async `on_commit`, `select_for_update`.
- [x] Async signal receivers and `async_to_sync` inside native code.
- [x] Opt-in `NativeManager`/`NativeModelMixin` for `PATCH_ORM = False`.
- [x] Non-PostgreSQL aliases raise `NativeAsyncUnsupported`, or fall back with `SyncFallbackWarning`.
- [x] ContentType cache warmed at startup for generic relations.
- [x] Native `SessionStore.asave()` for the db and cached_db session backends (the only DB-bound contrib async method left on a thread; `BaseBackend` auth defaults and `acheck_password` stay threaded on purpose).
- [x] `COPY` through Django's cursor: `cursor.copy()` returns a synchronous facade over psycopg's `AsyncCopy` (`write`, `write_row`, `read`, `read_row`, `rows`, iteration); it joins an open native transaction.

## 2. ASGI and REST

- [x] `NativeASGIHandler`: request signals, rendering, exception handling, logging, and `response.close()` without threads; thread-bound requests still close their thread's connections.
- [x] `ATOMIC_REQUESTS` for async views; opt-in native sync middleware; system checks for sync-only middleware and non-native aliases.
- [x] Django's non-blocking built-in middleware runs natively (MiddlewareMixin hooks); async `process_exception` hooks run natively; check W003 for thread-bound MiddlewareMixin middleware.
- [x] User guide (`docs/guide.md`), including async service clients and middleware.
- [x] `asyncdjango.rest_framework` with ADRF's API: APIView, generics, mixins, viewsets, routers, serializers (async method fields, nested, many), `@api_view`; sync and async auth/permission/throttle/filter/paginator hooks.
- [x] Test helpers: `NativeTestCase` (rollback), `NativeTransactionTestCase` (flush), in-process `ASGIClient`.
- [x] Django Channels: document and test routing the `http` and `lifespan` scopes to `NativeASGIHandler`; websocket consumers and middleware on the native ORM.

## 3. Compatibility and release

- [x] Compare correctness and concurrency against Django, DRF, and ADRF under the same PostgreSQL workload (`benchmarks/`, five stacks, `--repeat` medians, README charts; identical response fingerprints).
- [x] `asyncdjango-drf` at 100 clients is not bimodal behind a real server on Linux: rounds of 202-220 req/s (read) and 245-250 (write). The 67-264 spread came from the in-process Windows harness, where the load generator shares the event loop with the stack.
- [x] Document configuration, migrations, deployment, security, and supported versions (`docs/deployment.md`, `docs/compatibility.md`, `docs/porting.md`).
- [ ] Port a production ADRF application and run its test suite. Ported on 2026-10-28 (a CRM's `feat/asyncdjango` branch: adrf imports replaced, 424 of 490 `sync_to_async` calls now `ais_valid`/`sync_to_native`, WhiteNoise limited to static paths, `search_path` tenancy kept per pooled connection, blocking calls moved off the loop); its test suite has not been run yet.
- [x] GitHub Actions workflow: suite on Python 3.12-3.14 x PostgreSQL 14/18 service containers; optional benchmark job.
- [x] Linux CI is green: Python 3.12/3.13/3.14 x PostgreSQL 14/18 on every push to `main` since 2026-10-28.
- [x] Real-server benchmark on Linux: `benchmarks/server.py` (uvicorn/gunicorn + wrk, CPU-pinned) and the `server benchmark` workflow; results in `docs/benchmarks.md`.
- [x] One-core server benchmark with peak memory (2026-10-29): plain asyncdjango leads every stack; asyncdjango.rest_framework trails synchronous DRF by 15-22% on CPU-bound endpoints because its DRF layer costs 17-28% more CPU per request.
- [ ] Profile and cut the per-request CPU of `asyncdjango.rest_framework` on the read and write endpoints, to at least match synchronous DRF on one core.
- [x] Support Django 5.2 LTS: the full suite passes on 5.2.18, every adapted function is identical to 6.0.3 apart from comments, and CI runs 5.2 on Python 3.12/PostgreSQL 14 and Python 3.14/PostgreSQL 18.
- [ ] Define release criteria for 1.0 (a production application running on it in staging). A starting point to decide on: the CRM port's test suite passes; it runs in staging under real traffic for an agreed period without pool exhaustion, cross-tenant reads, or event-loop stalls; CI stays green on every supported Python, Django, and PostgreSQL version; and the public API (`sync_to_native`, settings, `asyncdjango.rest_framework`) is frozen with a deprecation policy.
