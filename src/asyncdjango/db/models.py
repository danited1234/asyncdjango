"""First slice of Django-style model declarations and lazy query building.

This module compiles parameterized SELECT statements. It does not execute them
or provide Django's full field, relation, or queryset APIs yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Any, AsyncIterator, Protocol

from .postgres import TransactionConnection
from .transaction import current_database


_creation_order = count()
_UNSET = object()
CASCADE = "CASCADE"
RESTRICT = "RESTRICT"
SET_NULL = "SET NULL"


def _quote(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("SQL identifier must be a nonempty string")
    return '"' + name.replace('"', '""') + '"'


class Field:
    def __init__(
        self,
        *,
        primary_key: bool = False,
        null: bool = False,
        unique: bool = False,
        default: Any = _UNSET,
        db_default: Any = _UNSET,
        db_column: str | None = None,
    ) -> None:
        self.primary_key = primary_key
        self.null = null
        self.unique = unique
        self.default = default
        self.db_default = db_default
        self.db_column = db_column
        self.creation_order = next(_creation_order)
        self.name = ""

    @property
    def column(self) -> str:
        return self.db_column or self.name


class IntegerField(Field):
    pass


class AutoField(IntegerField):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(primary_key=True, **kwargs)


class BooleanField(Field):
    pass


class DateTimeField(Field):
    pass


class CharField(Field):
    def __init__(self, *, max_length: int, **kwargs: Any) -> None:
        if max_length < 1:
            raise ValueError("max_length must be positive")
        self.max_length = max_length
        super().__init__(**kwargs)


class UnloadedRelationError(RuntimeError):
    """Raised when a relationship would otherwise trigger implicit I/O."""


class ForeignKey(Field):
    def __init__(self, to: type[Model], on_delete: str,
                 **kwargs: Any) -> None:
        if on_delete not in {CASCADE, RESTRICT, SET_NULL}:
            raise ValueError("Unsupported on_delete action")
        if on_delete == SET_NULL and not kwargs.get("null", False):
            raise ValueError("SET NULL requires null=True")
        self.to = to
        self.on_delete = on_delete
        super().__init__(**kwargs)

    @property
    def column(self) -> str:
        return self.db_column or f"{self.name}_id"

    def __get__(self, instance: Model | None, owner: type[Model]) -> Model | None | ForeignKey:
        if instance is None:
            return self
        if self.name not in instance._related_cache:
            raise UnloadedRelationError(
                f"{self.name} is not loaded; use select_related({self.name!r})"
            )
        return instance._related_cache[self.name]

    def __set__(self, instance: Model, value: Model | None) -> None:
        if value is not None and not isinstance(value, self.to):
            raise TypeError(f"{self.name} requires {self.to.__name__} or None")
        if value is not None and value.pk is None:
            raise ValueError("Save a related object before assigning it")
        setattr(instance, f"{self.name}_id", None if value is None else value.pk)
        instance._related_cache[self.name] = value


@dataclass(frozen=True)
class ModelOptions:
    db_table: str
    fields: dict[str, Field]
    pk_name: str


class DatabaseClient(Protocol):
    async def fetch(self, query: str, *args: Any) -> list[Any]: ...

    async def execute(self, query: str, *args: Any) -> str: ...

    def stream(self, query: str, *args: Any, batch_size: int = 100
               ) -> AsyncIterator[Any]: ...


class Q:
    """A composable WHERE expression using Django's `&`, `|`, and `~`."""

    def __init__(self, *children: Q, **lookups: Any) -> None:
        if any(not isinstance(child, Q) for child in children):
            raise TypeError("Positional Q children must be Q objects")
        self.children: tuple[Q | tuple[str, Any], ...] = (
            *children,
            *lookups.items(),
        )
        self.connector = "AND"
        self.negated = False

    def _combine(self, other: Q, connector: str) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        combined = Q(self, other)
        combined.connector = connector
        return combined

    def __and__(self, other: Q) -> Q:
        return self._combine(other, "AND")

    def __or__(self, other: Q) -> Q:
        return self._combine(other, "OR")

    def __invert__(self) -> Q:
        inverted = Q(self)
        inverted.negated = True
        return inverted


class QuerySet:
    def __init__(
        self,
        model: type[Model],
        conditions: tuple[Q, ...] = (),
        ordering: tuple[str, ...] = (),
        database: DatabaseClient | None = None,
        for_update: bool = False,
        related: tuple[str, ...] = (),
        low_mark: int = 0,
        high_mark: int | None = None,
    ) -> None:
        self.model = model
        self._conditions = conditions
        self._ordering = ordering
        self._database = database
        self._for_update = for_update
        self._related = related
        self._low_mark = low_mark
        self._high_mark = high_mark

    def all(self) -> QuerySet:
        return self._clone()

    def _clone(self, *, conditions: tuple[Q, ...] | None = None,
               ordering: tuple[str, ...] | None = None) -> QuerySet:
        return QuerySet(
            self.model,
            self._conditions if conditions is None else conditions,
            self._ordering if ordering is None else ordering,
            self._database,
            self._for_update,
            self._related,
            self._low_mark,
            self._high_mark,
        )

    def __getitem__(self, key: slice) -> QuerySet:
        if not isinstance(key, slice) or key.step is not None:
            raise TypeError("QuerySet supports only contiguous slices")
        start = 0 if key.start is None else key.start
        stop = key.stop
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise ValueError("Slice start must be a nonnegative integer")
        if stop is not None and (not isinstance(stop, int) or isinstance(stop, bool)
                                 or stop < start):
            raise ValueError("Slice stop must be at least the start")
        clone = self._clone()
        clone._low_mark = self._low_mark + start
        clone._high_mark = (self._low_mark + stop if stop is not None else self._high_mark)
        if self._high_mark is not None:
            clone._high_mark = min(clone._high_mark, self._high_mark) if clone._high_mark is not None else self._high_mark
        return clone

    def using(self, database: DatabaseClient) -> QuerySet:
        """Bind this query to a pool or an active transaction connection."""
        clone = self._clone()
        clone._database = database
        return clone

    def select_for_update(self) -> QuerySet:
        clone = self._clone()
        clone._for_update = True
        return clone

    def select_related(self, *names: str) -> QuerySet:
        for name in names:
            if not isinstance(self.model._meta.fields.get(name), ForeignKey):
                raise ValueError(f"Not a foreign key field: {name}")
        clone = self._clone()
        clone._related = tuple(dict.fromkeys((*self._related, *names)))
        return clone

    def _require_database(self) -> DatabaseClient:
        database = self._database or current_database()
        if database is None:
            raise RuntimeError("Bind a database with .using(database) before I/O")
        if self._for_update and not isinstance(database, TransactionConnection):
            raise RuntimeError("select_for_update requires transaction.atomic()")
        return database

    def filter(self, *conditions: Q, **lookups: Any) -> QuerySet:
        if any(not isinstance(condition, Q) for condition in conditions):
            raise TypeError("Positional filter conditions must be Q objects")
        additions = conditions + ((Q(**lookups),) if lookups else ())
        return self._clone(conditions=self._conditions + additions)

    def exclude(self, *conditions: Q, **lookups: Any) -> QuerySet:
        if not conditions and not lookups:
            return self._clone()
        return self.filter(~Q(*conditions, **lookups))

    def order_by(self, *fields: str) -> QuerySet:
        for field in fields:
            name = field[1:] if field.startswith("-") else field
            if name not in self.model._meta.fields:
                raise ValueError(f"Unknown ordering field: {name}")
        return self._clone(ordering=fields)

    def _compile_lookup(self, lookup: str, value: Any, params: list[Any]) -> str:
        bits = lookup.split("__")
        if len(bits) > 2:
            raise ValueError(f"Related or nested lookup is not supported: {lookup}")
        field_name = self.model._meta.pk_name if bits[0] == "pk" else bits[0]
        if field_name.endswith("_id") and field_name[:-3] in self.model._meta.fields:
            candidate = self.model._meta.fields[field_name[:-3]]
            if isinstance(candidate, ForeignKey):
                field_name = field_name[:-3]
        field = self.model._meta.fields.get(field_name)
        if field is None:
            raise ValueError(f"Unknown model field: {bits[0]}")
        operation = bits[1] if len(bits) == 2 else "exact"
        column = (f'"t0".{_quote(field.column)}' if self._related else
                  _quote(field.column))
        if isinstance(field, ForeignKey) and isinstance(value, Model):
            value = value.pk
        if operation == "isnull":
            if not isinstance(value, bool):
                raise TypeError("isnull requires a bool")
            return f"{column} IS {'NULL' if value else 'NOT NULL'}"
        if operation == "exact" and value is None:
            return f"{column} IS NULL"
        if operation == "in":
            if isinstance(value, (str, bytes)):
                raise TypeError("in lookup requires an iterable of values")
            values = [item.pk if isinstance(field, ForeignKey)
                      and isinstance(item, Model) else item for item in value]
            if not values:
                return "FALSE"
            placeholders = []
            for item in values:
                params.append(item)
                placeholders.append(f"${len(params)}")
            return f"{column} IN ({', '.join(placeholders)})"
        operators = {
            "exact": "=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=",
        }
        if operation not in operators:
            raise ValueError(f"Unsupported lookup: {operation}")
        params.append(value)
        return f"{column} {operators[operation]} ${len(params)}"

    def _compile_q(self, condition: Q, params: list[Any]) -> str:
        fragments = []
        for child in condition.children:
            if isinstance(child, Q):
                fragment = self._compile_q(child, params)
            else:
                fragment = self._compile_lookup(child[0], child[1], params)
            if fragment:
                fragments.append(fragment)
        if not fragments:
            return ""
        joined = f" {condition.connector} ".join(fragments)
        return f"NOT ({joined})" if condition.negated else f"({joined})"

    def compile(self, *, limit: int | None = None) -> tuple[str, tuple[Any, ...]]:
        """Return SQL and parameters without making a database call."""
        fields = self.model._meta.fields.values()
        if self._related:
            columns = [f'"t0".{_quote(field.column)} AS {_quote(field.column)}'
                       for field in fields]
            joins = []
            for index, name in enumerate(self._related, 1):
                foreign_key = self.model._meta.fields[name]
                target = foreign_key.to
                alias = f"t{index}"
                target_pk = target._meta.fields[target._meta.pk_name]
                joins.append(
                    f'LEFT JOIN {_quote(target._meta.db_table)} AS {_quote(alias)} '
                    f'ON "t0".{_quote(foreign_key.column)} = '
                    f'{_quote(alias)}.{_quote(target_pk.column)}'
                )
                columns.extend(
                    f'{_quote(alias)}.{_quote(field.column)} AS '
                    f'{_quote(name + "__" + field.column)}'
                    for field in target._meta.fields.values()
                )
            sql = (f"SELECT {', '.join(columns)} FROM "
                   f"{_quote(self.model._meta.db_table)} AS \"t0\" "
                   + " ".join(joins))
        else:
            columns = ", ".join(_quote(field.column) for field in fields)
            sql = f"SELECT {columns} FROM {_quote(self.model._meta.db_table)}"
        params: list[Any] = []
        where = [self._compile_q(condition, params) for condition in self._conditions]
        where = [fragment for fragment in where if fragment]
        if where:
            sql += " WHERE " + " AND ".join(where)
        if self._ordering:
            order = []
            for name in self._ordering:
                descending = name.startswith("-")
                field = self.model._meta.fields[name[1:] if descending else name]
                column = (f'"t0".{_quote(field.column)}' if self._related else
                          _quote(field.column))
                order.append(column + (" DESC" if descending else " ASC"))
            sql += " ORDER BY " + ", ".join(order)
        effective_limit = limit if limit is not None else (
            None if self._high_mark is None else max(0, self._high_mark - self._low_mark)
        )
        if effective_limit is not None:
            if effective_limit < 0:
                raise ValueError("limit must be nonnegative")
            sql += f" LIMIT {effective_limit}"
        if self._low_mark:
            sql += f" OFFSET {self._low_mark}"
        if self._for_update:
            sql += " FOR UPDATE"
        return sql, tuple(params)

    def _model_from_row(self, row: Any) -> Model:
        values = {name: row[field.column]
                  for name, field in self.model._meta.fields.items()}
        instance = self.model(**values)
        instance._database = self._database or current_database()
        for name in self._related:
            foreign_key = self.model._meta.fields[name]
            target = foreign_key.to
            values = {field_name: row[f"{name}__{field.column}"]
                      for field_name, field in target._meta.fields.items()}
            if values[target._meta.pk_name] is None:
                setattr(instance, name, None)
            else:
                related = target(**values)
                related._database = instance._database
                setattr(instance, name, related)
        return instance

    async def fetch_all(self) -> list[Model]:
        sql, params = self.compile()
        rows = await self._require_database().fetch(sql, *params)
        return [self._model_from_row(row) for row in rows]

    async def _iterate(self) -> AsyncIterator[Model]:
        sql, params = self.compile()
        async for row in self._require_database().stream(sql, *params):
            yield self._model_from_row(row)

    def __aiter__(self) -> AsyncIterator[Model]:
        return self._iterate()

    async def get(self, *conditions: Q, **lookups: Any) -> Model:
        query = self.filter(*conditions, **lookups)
        sql, params = query.compile(limit=2)
        rows = await query._require_database().fetch(sql, *params)
        if not rows:
            raise self.model.DoesNotExist(
                f"{self.model.__name__} matching query does not exist"
            )
        if len(rows) > 1:
            raise self.model.MultipleObjectsReturned(
                f"get() returned more than one {self.model.__name__}"
            )
        return query._model_from_row(rows[0])

    def _where(self, params: list[Any]) -> str:
        fragments = [self._compile_q(condition, params)
                     for condition in self._conditions]
        fragments = [fragment for fragment in fragments if fragment]
        return " WHERE " + " AND ".join(fragments) if fragments else ""

    async def count(self) -> int:
        params: list[Any] = []
        sql = (f"SELECT COUNT(*) AS n FROM {_quote(self.model._meta.db_table)}"
               + self._where(params))
        rows = await self._require_database().fetch(sql, *params)
        return rows[0]["n"]

    async def exists(self) -> bool:
        params: list[Any] = []
        sql = (f"SELECT 1 AS n FROM {_quote(self.model._meta.db_table)}"
               + self._where(params) + " LIMIT 1")
        rows = await self._require_database().fetch(sql, *params)
        return bool(rows)

    async def create(self, **values: Any) -> Model:
        instance = self.model(**values)
        fields = [field for field in self.model._meta.fields.values()
                  if (not isinstance(field, AutoField) or getattr(instance, field.name) is not None)
                  and field.name not in instance._db_default_pending]
        returning = ", ".join(_quote(field.column)
                              for field in self.model._meta.fields.values())
        if fields:
            columns = ", ".join(_quote(field.column) for field in fields)
            placeholders = ", ".join(f"${number}" for number in range(1, len(fields) + 1))
            sql = (f"INSERT INTO {_quote(self.model._meta.db_table)} "
                   f"({columns}) VALUES ({placeholders}) RETURNING {returning}")
        else:
            sql = (f"INSERT INTO {_quote(self.model._meta.db_table)} "
                   f"DEFAULT VALUES RETURNING {returning}")
        params = [_field_value(instance, field) for field in fields]
        rows = await self._require_database().fetch(sql, *params)
        if len(rows) != 1:
            raise RuntimeError("INSERT did not return one row")
        return self._model_from_row(rows[0])

    async def update(self, **values: Any) -> int:
        if not values:
            return 0
        fields = {}
        for name in values:
            field_name = name[:-3] if name.endswith("_id") and isinstance(
                self.model._meta.fields.get(name[:-3]), ForeignKey
            ) else name
            if field_name not in self.model._meta.fields:
                raise ValueError(f"Unknown model field: {name}")
            if self.model._meta.fields[field_name].primary_key:
                raise ValueError("Updating the primary key is not supported")
            fields[name] = self.model._meta.fields[field_name]
        params = [value.pk if isinstance(value, Model) else value
                  for value in values.values()]
        assignments = ", ".join(
            f"{_quote(fields[name].column)} = ${index}"
            for index, name in enumerate(values, 1)
        )
        sql = (f"UPDATE {_quote(self.model._meta.db_table)} SET {assignments}"
               + self._where(params))
        status = await self._require_database().execute(sql, *params)
        return int(status.rsplit(" ", 1)[-1])

    async def delete(self) -> int:
        params: list[Any] = []
        sql = f"DELETE FROM {_quote(self.model._meta.db_table)}" + self._where(params)
        status = await self._require_database().execute(sql, *params)
        return int(status.rsplit(" ", 1)[-1])


class _ManagerDescriptor:
    def __get__(self, instance: Model | None, owner: type[Model]) -> QuerySet:
        if instance is not None:
            raise AttributeError("objects is available on the model class")
        return QuerySet(owner)


def _field_value(instance: Model, field: Field) -> Any:
    return (getattr(instance, f"{field.name}_id") if isinstance(field, ForeignKey)
            else getattr(instance, field.name))


class ModelBase(type):
    def __new__(mcls, name: str, bases: tuple[type, ...], attrs: dict[str, Any]):
        meta = attrs.pop("Meta", None)
        cls = super().__new__(mcls, name, bases, attrs)
        if name == "Model" and cls.__module__ == __name__:
            return cls

        fields: dict[str, Field] = {}
        for base in bases:
            if hasattr(base, "_meta"):
                fields.update(base._meta.fields)
        fields.update({key: value for key, value in attrs.items()
                       if isinstance(value, Field)})
        if not any(field.primary_key for field in fields.values()):
            if "id" in fields:
                raise TypeError("An explicit id field must be the primary key")
            fields = {"id": AutoField(), **fields}
        primary_keys = [key for key, field in fields.items() if field.primary_key]
        if len(primary_keys) != 1:
            raise TypeError("A model must have exactly one primary key field")
        columns = [field.db_column or key for key, field in fields.items()]
        if len(set(columns)) != len(columns):
            raise TypeError("Model fields must use distinct database columns")
        for key, field in fields.items():
            field.name = key
        table = getattr(meta, "db_table", None)
        if table is None:
            table = f"{cls.__module__.rsplit('.', 1)[-1]}_{name.lower()}"
        _quote(table)
        cls._meta = ModelOptions(table, fields, primary_keys[0])
        cls.DoesNotExist = type("DoesNotExist", (LookupError,), {})
        cls.MultipleObjectsReturned = type("MultipleObjectsReturned", (LookupError,), {})
        return cls


class Model(metaclass=ModelBase):
    objects = _ManagerDescriptor()

    def __init__(self, **values: Any) -> None:
        self._database: DatabaseClient | None = None
        self._related_cache: dict[str, Model | None] = {}
        self._db_default_pending: set[str] = set()
        allowed = set(self._meta.fields)
        allowed.update(f"{name}_id" for name, field in self._meta.fields.items()
                       if isinstance(field, ForeignKey))
        unknown = values.keys() - allowed
        if unknown:
            raise TypeError(f"Unknown model fields: {', '.join(sorted(unknown))}")
        for name, field in self._meta.fields.items():
            id_name = f"{name}_id"
            if (name not in values and id_name not in values
                    and field.default is _UNSET and field.db_default is not _UNSET):
                self._db_default_pending.add(name)
            if isinstance(field, ForeignKey):
                if name in values and id_name in values:
                    raise TypeError(f"Specify either {name} or {id_name}")
                if name in values and isinstance(values[name], Model):
                    setattr(self, name, values[name])
                    continue
                value = values[name] if name in values else values.get(id_name, None)
                if value is not None:
                    setattr(self, id_name, value)
                else:
                    setattr(self, name, None)
                continue
            if name in values:
                value = values[name]
            elif field.default is _UNSET:
                value = None
            else:
                value = field.default() if callable(field.default) else field.default
            setattr(self, name, value)

    @property
    def pk(self) -> Any:
        return getattr(self, self._meta.pk_name)

    @pk.setter
    def pk(self, value: Any) -> None:
        setattr(self, self._meta.pk_name, value)

    def using(self, database: DatabaseClient) -> Model:
        self._database = database
        return self

    async def save(self) -> None:
        # The task's active transaction takes precedence over an instance's
        # old binding, which may refer to a transaction that has already ended.
        database = current_database() or self._database
        if database is None:
            raise RuntimeError("Bind a database with .using(database) before I/O")
        query = type(self).objects.using(database)
        if self.pk is None:
            created = await query.create(**{
                name: _field_value(self, self._meta.fields[name])
                for name in self._meta.fields
                if name != self._meta.pk_name and name not in self._db_default_pending
            })
            for name in self._meta.fields:
                setattr(self, name, getattr(created, name))
            self._db_default_pending.clear()
        else:
            await query.filter(**{self._meta.pk_name: self.pk}).update(**{
                name: _field_value(self, self._meta.fields[name])
                for name in self._meta.fields
                if name != self._meta.pk_name and name not in self._db_default_pending
            })
