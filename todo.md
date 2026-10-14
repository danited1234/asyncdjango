# TODO

## Project rules

- [x] Define the supported Django/DRF syntax and explicit incompatibilities in a compatibility matrix.
- [ ] Pin a Django source version before reusing internals; preserve copyright notices and review its license obligations.
- [ ] Keep the normal request and database path free of `sync_to_async`, `asyncio.to_thread`, and executor calls.
- [ ] Make every operation that can perform I/O explicit with `await` or `async with`.
- [ ] Prove behavior with tests and inspect the actual driver path; an `async def` signature alone is not proof of native async I/O.

## 0. Async PostgreSQL foundation

- [x] Create package, repository, and native `asyncpg` connection spike.
- [x] Run the PostgreSQL integration tests for concurrent queries and cancellation against a live database.
- [x] Add connection pool lifecycle hooks for ASGI startup and shutdown.
- [x] Define task ownership of transaction connections; reject access from another task.
- [x] Add nested transaction and rollback-only APIs; verify both against PostgreSQL.
- [x] Ensure cancelled database transactions roll back and release connections (HTTP disconnect handling remains open).

## 1. Django-style ORM vertical slice

- [x] Model metaclass, field declarations, model metadata, and table naming (initial field subset).
- [x] Lazy queryset construction with `filter`, `exclude`, `Q`, `order_by`, and SQL parameters (initial lookup subset).
- [x] Native awaited `get`, `fetch_all`, `count`, `exists`, `create`, `update`, `delete`, and model `save` (initial PostgreSQL subset).
- [x] Async iteration with bounded cursor prefetch; verify memory usage on a 50,000-row result set.
- [x] Single-level foreign keys and `select_related`; reject implicit lazy relation I/O.
- [ ] Reverse relations, many-to-many fields, and multi-level `select_related`.
- [x] Unique constraints, Python defaults, basic field validation, and duplicate-write error mapping.
- [ ] SQL defaults, complete field validation, and full database error mapping.
- [x] `async with transaction.atomic()` with nested savepoints and `select_for_update` (initial PostgreSQL implementation).
- [x] Generate and apply tracked initial CREATE TABLE migrations for the supported field subset.
- [ ] Generate ALTER/DROP migrations for changes to existing models.

## 2. ASGI and REST vertical slice

- [x] Async JSON request/response and static/dynamic routing with startup/shutdown lifecycle.
- [x] Cancel an in-flight handler and database transaction on client disconnect.
- [ ] Add an async middleware pipeline and streaming request/response bodies.
- [x] Initial DRF-style `APIView`, router, and `ModelViewSet` with `async def` handlers.
- [x] Initial model serializer field validation and async writes.
- [x] Async uniqueness and foreign-key existence validators.
- [ ] Full DRF serializer compatibility, including nested writes and custom field hooks.
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
