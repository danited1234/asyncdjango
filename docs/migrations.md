# PostgreSQL migrations

`initial_migration(name, models)` generates the supported `CREATE TABLE`
statements. `alter_migration(name, old_models, new_models)` compares model
definitions for the **same existing tables** and generates column additions,
type changes, SQL-default changes, nullability changes, and column drops.
`apply_migration(database, migration)` executes every statement in one native
async PostgreSQL transaction and records its checksum in
`_asyncdjango_migrations`. Reapplying the same name and SQL returns `False`;
changing SQL behind an applied name raises an error.

For example, keep the old model definition available while generating the
next migration:

```python
from asyncdjango.db.migrations import alter_migration, apply_migration

migration = alter_migration("0002_change_user", [OldUser], [NewUser])
await apply_migration(database, migration)
```

Dropping a column or narrowing/changing its type requires
`allow_destructive=True`. This is only an explicit opt-in to generate SQL;
**back up production data and review the generated statements before applying**.
A new `NOT NULL` column without a SQL default may fail on a populated table.
If PostgreSQL rejects any statement, the whole migration rolls back, including
its tracking row. Long-running ALTERs may lock tables; schedule them for an
appropriate maintenance window.

Declare `db_default=` on a model field for a database-generated default.
Literal strings, integers, booleans, datetimes, and `None` are supported;
`SQLExpression("CURRENT_TIMESTAMP")` and `SQLExpression("NOW()")` are the
supported server expressions. Omitted fields use the database default in
native ORM inserts and serializer writes. Python `default=` still takes
precedence for ORM-created objects when both are set.

This is not yet Django's migration system. It does not automatically detect
renames, add/drop tables, or safely rewrite unique, primary-key, and
foreign-key constraints. Those changes currently fail closed and require an
explicit, reviewed migration. Migration dependencies and rollback files are
not generated yet.
