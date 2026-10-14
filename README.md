# asyncdjango

An experimental async-native framework aiming for Django and Django REST Framework-style application code. This repository is at the foundation stage; it is **not** a Django replacement yet.

The central rule is that request-time I/O must await an async driver directly. The normal database path must not use `sync_to_async`, `asyncio.to_thread`, or an executor. CPU-only work such as building a query may stay synchronous.

## Current scope

The first code is a PostgreSQL foundation built on `asyncpg`. It has an async connection pool, transactions, Django-style model declarations, lazy SQL construction, and an initial native async CRUD API. Initial table migrations, a JSON ASGI router, serializers, and model viewsets are implemented. The broader Django/DRF feature set is still in progress.

The ASGI `DatabaseLifespan` wrapper opens and closes the pool at application startup and shutdown. A transaction yields a task-owned connection; use its methods inside that transaction. Nested transaction blocks create PostgreSQL savepoints. `set_rollback()` rolls back the current transaction level when its block exits.

Single-level foreign keys are supported. Use `select_related("organization")` to load one; reading an unloaded relation raises `UnloadedRelationError` so it cannot trigger an implicit database query.

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
