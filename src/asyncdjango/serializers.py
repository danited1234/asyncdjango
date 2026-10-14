"""Initial DRF-style model serializer for the supported field subset."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from .db.models import (
    AutoField, BooleanField, CharField, DateTimeField, ForeignKey, IntegerField,
    Model, _UNSET,
)


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__(str(errors))


class ModelSerializer:
    class Meta:
        model: type[Model]
        fields: tuple[str, ...]

    def __init__(
        self, instance: Model | list[Model] | None = None,
        *, data: dict[str, Any] | None = None,
        many: bool = False,
    ) -> None:
        self.instance = instance
        self.initial_data = data
        self.many = many
        self.validated_data: dict[str, Any] | None = None
        self.errors: dict[str, str] = {}
        model = self.Meta.model
        for name in self.Meta.fields:
            if name not in model._meta.fields and name != "pk":
                raise ValueError(f"Unknown serializer field: {name}")

    @property
    def data(self) -> Any:
        if self.instance is None:
            raise RuntimeError("Serialize an instance after asave()")
        if self.many:
            return [self._represent(item) for item in self.instance]
        return self._represent(self.instance)

    def _represent(self, instance: Model) -> dict[str, Any]:
        result = {}
        for name in self.Meta.fields:
            field = instance._meta.fields.get(name)
            value = (getattr(instance, f"{name}_id") if isinstance(field, ForeignKey)
                     else getattr(instance, name))
            result[name] = value.isoformat() if isinstance(value, datetime) else value
        return result

    def is_valid(self, *, raise_exception: bool = False) -> bool:
        if not isinstance(self.initial_data, dict):
            self.errors = {"non_field_errors": "Expected an object"}
            if raise_exception:
                raise ValidationError(self.errors)
            return False
        self.errors = {}
        values = {}
        model = self.Meta.model
        allowed = {name for name in self.Meta.fields
                   if name != "pk" and not isinstance(model._meta.fields[name], AutoField)}
        for name in self.initial_data.keys() - allowed:
            self.errors[name] = "Unknown or read-only field"
        for name in allowed:
            field = model._meta.fields[name]
            if isinstance(field, AutoField):
                continue
            if name not in self.initial_data:
                if self.instance is None and field.default is _UNSET and not field.null:
                    self.errors[name] = "This field is required"
                continue
            value = self.initial_data[name]
            if value is None:
                if not field.null:
                    self.errors[name] = "This field may not be null"
                else:
                    values[name] = None
            elif isinstance(field, CharField):
                if not isinstance(value, str) or len(value) > field.max_length:
                    self.errors[name] = f"Expected a string of at most {field.max_length} characters"
                else:
                    values[name] = value
            elif isinstance(field, BooleanField):
                if not isinstance(value, bool):
                    self.errors[name] = "Expected a boolean"
                else:
                    values[name] = value
            elif isinstance(field, IntegerField):
                if isinstance(value, bool) or not isinstance(value, int):
                    self.errors[name] = "Expected an integer"
                else:
                    values[name] = value
            elif isinstance(field, DateTimeField):
                try:
                    values[name] = datetime.fromisoformat(value) if isinstance(value, str) else value
                    if not isinstance(values[name], datetime):
                        raise ValueError
                except ValueError:
                    self.errors[name] = "Expected an ISO 8601 datetime"
            elif isinstance(field, ForeignKey):
                target_pk = field.to._meta.fields[field.to._meta.pk_name]
                if isinstance(target_pk, IntegerField):
                    valid = isinstance(value, int) and not isinstance(value, bool)
                else:
                    valid = isinstance(value, str)
                if not valid:
                    self.errors[name] = "Expected a related primary key"
                else:
                    values[name] = value
        self.validated_data = values if not self.errors else None
        if self.errors and raise_exception:
            raise ValidationError(self.errors)
        return not self.errors

    async def ais_valid(self, *, raise_exception: bool = False) -> bool:
        """Validate fields and check unique values with native async queries."""
        if not self.is_valid():
            if raise_exception:
                raise ValidationError(self.errors)
            return False
        model = self.Meta.model
        for name, value in self.validated_data.items():
            field = model._meta.fields[name]
            if isinstance(field, ForeignKey) and value is not None:
                if not await field.to.objects.filter(pk=value).exists():
                    self.errors[name] = "Related object does not exist"
                    continue
            if not field.unique or value is None:
                continue
            query = model.objects.filter(**{name: value})
            if self.instance is not None and self.instance.pk is not None:
                query = query.exclude(pk=self.instance.pk)
            if await query.exists():
                self.errors[name] = "A row with this value already exists"
        if self.errors:
            self.validated_data = None
            if raise_exception:
                raise ValidationError(self.errors)
            return False
        return True

    async def asave(self) -> Model:
        if self.validated_data is None:
            raise RuntimeError("Call is_valid() before asave()")
        if self.instance is None:
            try:
                self.instance = await self.Meta.model.objects.create(**self.validated_data)
            except asyncpg.UniqueViolationError as exc:
                raise ValidationError({"non_field_errors": "Unique constraint violated"}) from exc
        else:
            for name, value in self.validated_data.items():
                setattr(self.instance, name, value)
            try:
                await self.instance.save()
            except asyncpg.UniqueViolationError as exc:
                raise ValidationError({"non_field_errors": "Unique constraint violated"}) from exc
        return self.instance
