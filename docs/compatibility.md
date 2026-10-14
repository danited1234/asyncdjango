# Compatibility target

This is the initial API contract. It describes intended behavior, not features
already shipped. The implementation status is tracked in `todo.md`.

| Django / DRF concept | Intended syntax | Asyncdjango rule |
| --- | --- | --- |
| Model declarations and fields | `class User(models.Model)` | Keep field declarations and `Meta` conventions where possible. |
| Query construction | `User.objects.filter(active=True).order_by("id")` | Synchronous and lazy; no database I/O. |
| Query evaluation | `await User.objects.get(pk=1)` | Explicit `await`; no `aget` alias required. |
| Iteration | `async for user in queryset` | Fetch in bounded batches. |
| Model writes | `await user.save()` | Explicit `await`. |
| Transactions | `async with transaction.atomic():` | Awaited begin/commit/rollback; nested blocks use savepoints. |
| Relations | `select_related()` or explicit awaited load | No implicit database query on ordinary attribute access. |
| Serializers | DRF-style field declarations and `Meta` | Async database-backed validation and writes. |
| Views and viewsets | `async def get/list/create(...)` | Native ASGI handlers. |
| Migrations | Django-like model migration workflow | Dedicated migration runner for supported fields. |

## Known incompatibilities by design

- A synchronous ORM call such as `User.objects.get(...)` cannot perform database I/O in an async request. It returns an awaitable in the target API.
- A lazy relation cannot silently fetch from the database when its attribute is read.
- Synchronous third-party Django apps cannot be assumed compatible. Compatibility must be tested package by package.
- PostgreSQL is the first supported database. Other Django database backends are outside the first release.
- The Django admin, forms, and templates are outside the first vertical slice.

No Django source has been copied into this repository. Before doing so, pin the exact upstream revision and keep its license and notices with the copied material.
