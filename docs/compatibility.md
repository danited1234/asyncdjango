# Compatibility

asyncdjango keeps Django and DRF syntax. Models, managers, querysets,
migrations, admin, and settings are Django's own. What changes is *where* the
async API runs: on the event loop thread, with database I/O awaited through
psycopg's async driver, instead of on a worker thread.

## Native on the event loop

With `"asyncdjango"` in `INSTALLED_APPS` (and `PATCH_ORM`, the default):

| API | Notes |
| --- | --- |
| `QuerySet`: `aget`, `acreate`, `aget_or_create`, `aupdate_or_create`, `abulk_create`, `abulk_update`, `acount`, `ain_bulk`, `aiterator`, `alatest`, `aearliest`, `afirst`, `alast`, `aaggregate`, `aexists`, `acontains`, `aupdate`, `adelete`, `aexplain` | Django's exact semantics: the synchronous method runs in a greenlet. |
| `async for obj in queryset` | Includes `values()`, `values_list()`, `select_related()`, `prefetch_related()`, annotations, raw querysets. |
| `aiterator(chunk_size=...)` | Streams through a server-side cursor (unless `DISABLE_SERVER_SIDE_CURSORS`), with `prefetch_related` per chunk. |
| `Model.asave()`, `adelete()`, `arefresh_from_db()` | Signals, `save()` overrides, cascades, and `on_delete` handlers run as in sync Django. |
| Related managers: `aadd`, `aremove`, `aclear`, `aset`, `acreate`, `aget_or_create`, `aupdate_or_create` | Reverse foreign keys, many-to-many, and generic relations. |
| `async with transaction.atomic()` | Django's `transaction.atomic` works with `async with` and on `async def`; nesting uses savepoints; `durable`, `set_rollback()`, `get_rollback()`. |
| `transaction.on_commit()` | Synchronous callbacks via Django's function; `asyncdjango.db.transaction.on_commit()` also accepts `async def` callbacks. |
| `select_for_update()` | Inside `atomic()`, as in Django. |
| Signal receivers | Synchronous receivers that query run natively; async receivers connected to `post_save` etc. are awaited. |
| `connection_created`, `connection.execute_wrapper()` | Work per pooled connection checkout (for example, `search_path` multi-tenancy). |
| `sync_to_native(fn)` | Runs any ORM-only synchronous function natively; a replacement for `sync_to_async` around ORM code. |
| `NativeASGIHandler` | Django's request cycle; see the README for what moves off threads. |
| `asyncdjango.rest_framework` | ADRF's API (`APIView`, generics, mixins, viewsets, routers, serializers, `@api_view`). |

## Behavior that differs from sync Django or ADRF

- **Synchronous hooks run on the event loop.** Code reached through the native
  path (model `save()` overrides, signal receivers, DRF authentication,
  permissions, throttles, filter backends, serializer fields and validators)
  runs on the event-loop thread. Database access in it is awaited; any other
  blocking call (HTTP clients, `time.sleep`, synchronous Redis or cache
  clients, large file reads) stalls every request on that worker. ADRF would
  have run such code in a thread. Make those hooks `async def` or move the
  blocking work out; `asyncdjango.diagnostics.EventLoopBlockDetector` finds
  stalls in tests.
- **Implicit lazy loading.** Reading an unloaded relation in *async* code
  still raises `SynchronousOnlyOperation`, as in Django. In code that runs
  natively (DRF serializer fields, `sync_to_native`), it becomes an awaited
  query, as in sync Django. Use `select_related()`/`prefetch_related()` to
  avoid N+1 queries either way.
- **Async generators.** Close async generators that open
  `transaction.atomic()` or iterate with `aiterator()` (use
  `contextlib.aclosing()`), as with any context-managed resource. An
  `atomic()` block inside an async generator applies only to code running in
  that generator; one abandoned mid-iteration is rolled back when the event
  loop finalizes it, with a warning. While `aiterator()` runs, ORM calls in the
  loop body reuse its connection, like one thread's connection in sync Django.
- **Error reporting stays on a thread.** 5xx responses and unexpected
  exceptions are logged and converted through Django's thread-based path,
  because logging handlers such as `AdminEmailHandler` send mail
  synchronously. 404, 403, and 400 responses stay on the event loop.
- **Async signal receivers** reached from native code (for example an
  `async def` `post_save` receiver during `asave()`) are awaited one after
  another in the sending task and join its transaction. Django would run
  them concurrently in a separate event loop.
- **Connections are task-scoped.** Outside a transaction each awaited ORM call
  borrows a pooled connection and returns it. Inside `async with atomic()` the
  task owns one connection until the block exits. Child tasks created inside
  the block (`asyncio.gather`, `create_task`) cannot use that transaction and
  raise `CrossTaskConnectionError`; await the calls sequentially. Child tasks
  created outside a transaction get their own connections.
- **Context variables** set by synchronous code that runs natively (for
  example `translation.activate()` in a natively run middleware) are visible
  to the coroutines it awaits and to the caller afterwards, as with
  `sync_to_async`.
- **`CONN_MAX_AGE` is ignored on the native path.** The psycopg pool manages
  connection lifetime (`ASYNCDJANGO["POOL"]`: `max_lifetime`, `max_idle`).
  `CONN_HEALTH_CHECKS` enables the pool's connection check. Django's own
  synchronous connections (management commands, sync views) still follow
  `CONN_MAX_AGE`.
- **Session state on pooled connections.** `SET` statements persist on a
  pooled connection after it is returned, as with Django's own psycopg pool.
  Reapply session settings per checkout, for example from `connection_created`.
- **Mixed sync and async in one request.** Synchronous views and
  synchronous-only middleware still run in Django's per-request thread, and
  that thread's Django connection is separate from the native one: it cannot
  see an open native transaction, and vice versa.
- **`DEBUG` query log.** `connection.queries` is per native wrapper, so it is
  only visible inside the same native call (for example, within a
  `sync_to_native` function). Tools that read the thread's connection, such
  as the debug toolbar, do not see native queries.
- **Tests.** Django's `TestCase` isolates tests with a transaction on its sync
  connection, which native queries cannot see. Use
  `asyncdjango.test.NativeTestCase` (rollback isolation) or
  `NativeTransactionTestCase` (commit and truncate).
- **Generic relations.** Accessing a `GenericRelation` manager calls
  `ContentType.objects.get_for_model()`, which queries synchronously on a
  cache miss (also true in plain Django). `NativeASGIHandler` loads all
  content types into Django's cache at lifespan startup; elsewhere call
  `await asyncdjango.asgi.warm_content_types()` once.
- **`async_to_sync` inside native code** awaits on the current loop instead of
  raising "cannot use AsyncToSync in the same thread as an async event loop".

## Not supported natively

These raise `NativeAsyncUnsupported`, or fall back to Django's thread-based
implementation with `SyncFallbackWarning` when
`ASYNCDJANGO["ALLOW_SYNC_FALLBACK"]` is `True`:

- Database aliases that are not PostgreSQL with psycopg 3 (SQLite, MySQL,
  Oracle, psycopg2). Backends that subclass Django's PostgreSQL backend are
  wrapped, but only the stock backend is tested.
- `async with transaction.atomic(using=...)` on such an alias always raises.

Also not covered:

- `cursor.copy()` (COPY) through Django's cursor; use psycopg's async API on a
  raw connection.
- Django's session `SessionStore.asave()`, `contrib.auth` backends'
  `aauthenticate()`/`aget_user()` default implementations, and other contrib
  async APIs not listed above keep Django's thread-based behavior. Django's
  `ModelBackend` async methods are already built on the patched ORM.
- Synchronous iterators in `StreamingHttpResponse` are consumed in a thread by
  Django; use async iterators.

## Infrastructure caveats

- **Windows development:** psycopg async cannot use Windows' default
  `ProactorEventLoop`; the server must run a `SelectorEventLoop` (or develop
  under WSL/Docker). The test helpers select the right loop automatically.
  Linux and macOS need nothing special.
- **Host name resolution:** asyncio resolves host names in its default thread
  executor when psycopg opens a *new physical connection* (not per query). Use
  an IP address for `HOST` to avoid it entirely.
- **Query cancellation:** when a request is cancelled mid-query (client
  disconnect), psycopg cancels the server-side query. With libpq 17+ that is
  non-blocking; with older libpq (psycopg-binary 3.3.2 bundles 16.9) psycopg
  sends the cancel request from a thread.
