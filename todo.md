# TODO

## Project rules

- [x] Define the supported Django/DRF syntax and explicit incompatibilities in a compatibility matrix.
- [ ] Pin a Django source version before reusing internals; preserve copyright notices and review its license obligations.
- [ ] Keep the normal request and database path free of `sync_to_async`, `asyncio.to_thread`, and executor calls.
- [ ] Make every operation that can perform I/O explicit with `await` or `async with`.
- [ ] Prove behavior with tests and inspect the actual driver path; an `async def` signature alone is not proof of native async I/O.

## 0. Async PostgreSQL foundation

- [x] Create package, repository, and native `asyncpg` connection spike.
- [ ] Run the PostgreSQL integration tests for concurrent queries and cancellation against a live database.
- [x] Add connection pool lifecycle hooks for ASGI startup and shutdown.
- [x] Define task ownership of transaction connections; reject access from another task.
- [ ] Add nested transactions with savepoints and rollback-only behavior.
- [ ] Ensure cancelled requests roll back and release connections.

## 1. Django-style ORM vertical slice

- [ ] Model metaclass, field declarations, model metadata, and table naming.
- [ ] Lazy queryset construction with `filter`, `exclude`, `Q`, `order_by`, and SQL parameters.
- [ ] Native awaited `get`, `all`, `count`, `exists`, `create`, `update`, `delete`, and model `save`.
- [ ] Async iteration with bounded fetches; verify memory usage on large result sets.
- [ ] Foreign keys and `select_related`; define explicit lazy relation loading.
- [ ] Unique constraints, defaults, validation boundaries, and database error mapping.
- [ ] `async with transaction.atomic()` with nested savepoints and `select_for_update`.
- [ ] Migration generation and application for the supported model subset.

## 2. ASGI and REST vertical slice

- [ ] Async request/response and routing with startup/shutdown lifecycle.
- [ ] Async middleware pipeline and client disconnect cancellation.
- [ ] DRF-style `APIView`, routers, and `ModelViewSet` with `async def` handlers.
- [ ] Serializer fields and validation; async database-backed validation and writes.
- [ ] Authentication, permissions, pagination, filtering, and throttling.
- [ ] OpenAPI schema generation and test client.

## 3. Compatibility and release

- [ ] Port one small existing Django/ADRF endpoint and record every source change.
- [ ] Compare correctness and concurrency against Django/ADRF and a native async baseline under the same PostgreSQL workload.
- [ ] Add blocking-call detection in the development/test configuration.
- [ ] Document configuration, migrations, deployment, security, and supported Python/PostgreSQL versions.
- [ ] Define release criteria and publish only after the vertical slice passes integration tests.

## First useful milestone

A model can be declared with Django-like fields, migrated, queried through a native PostgreSQL driver, serialized, served by an ASGI view, and updated inside an async transaction. The complete request path has no worker-thread bridge.
