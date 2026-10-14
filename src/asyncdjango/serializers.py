"""Initial DRF-style model serializer for the supported field subset."""

from __future__ import annotations

from datetime import datetime
import inspect
from typing import Any

import asyncpg

from .db.models import (
    AutoField, BooleanField, CharField, DateTimeField, ForeignKey, IntegerField,
    Model, _UNSET,
)
from .db import transaction


class ValidationError(ValueError):
    def __init__(self, errors: Any) -> None:
        self.errors = errors
        super().__init__(str(errors))


class ModelSerializer:
    class Meta:
        model: type[Model]
        fields: tuple[str, ...]

    def __init__(
        self, instance: Model | list[Model] | None = None,
        *, data: dict[str, Any] | None = None,
        many: bool = False, partial: bool = False,
    ) -> None:
        self.instance = instance
        self.initial_data = data
        self.many = many
        self.partial = partial
        self.validated_data: dict[str, Any] | None = None
        self.errors: dict[str, Any] = {}
        self._nested_serializers: dict[str, ModelSerializer] = {}
        model = self.Meta.model
        for name in self.Meta.fields:
            if name not in model._meta.fields and name != "pk":
                raise ValueError(f"Unknown serializer field: {name}")

    @property
    def data(self) -> Any:
        if self.instance is None:
            raise RuntimeError("Serialize an instance after asave()")
        if self.many:
            return [self.to_representation(item) for item in self.instance]
        return self.to_representation(self.instance)

    def to_representation(self, instance: Model) -> dict[str, Any]:
        """Override this DRF-style hook for custom output representation."""
        result = {}
        for name in self.Meta.fields:
            field = instance._meta.fields.get(name)
            declared = getattr(type(self), name, None)
            if isinstance(field, ForeignKey) and isinstance(declared, ModelSerializer):
                related = getattr(instance, name)  # Explicit preload required.
                value = None if related is None else type(declared)(related).data
            else:
                value = (getattr(instance, f"{name}_id") if isinstance(field, ForeignKey)
                         else getattr(instance, name))
            result[name] = value.isoformat() if isinstance(value, datetime) else value
        return result

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        """Convert incoming primitives; override for custom input conversion."""
        if not isinstance(data, dict):
            raise ValidationError({"non_field_errors": "Expected an object"})
        errors: dict[str, Any] = {}
        values = {}
        model = self.Meta.model
        self._nested_serializers = {}
        allowed = {name for name in self.Meta.fields
                   if name != "pk" and not isinstance(model._meta.fields[name], AutoField)}
        for name in data.keys() - allowed:
            errors[name] = "Unknown or read-only field"
        for name in allowed:
            field = model._meta.fields[name]
            if isinstance(field, AutoField):
                continue
            if name not in data:
                if self.instance is None and not self.partial and field.default is _UNSET and not field.null:
                    errors[name] = "This field is required"
                continue
            value = data[name]
            declared = getattr(type(self), name, None)
            if value is None:
                if not field.null:
                    errors[name] = "This field may not be null"
                else:
                    values[name] = None
            elif isinstance(field, ForeignKey) and isinstance(declared, ModelSerializer):
                nested = type(declared)(data=value, partial=self.instance is not None)
                if not nested.is_valid():
                    errors[name] = nested.errors
                else:
                    self._nested_serializers[name] = nested
                    values[name] = nested.validated_data
            elif isinstance(field, CharField):
                if not isinstance(value, str) or len(value) > field.max_length:
                    errors[name] = f"Expected a string of at most {field.max_length} characters"
                else:
                    values[name] = value
            elif isinstance(field, BooleanField):
                if not isinstance(value, bool):
                    errors[name] = "Expected a boolean"
                else:
                    values[name] = value
            elif isinstance(field, IntegerField):
                if isinstance(value, bool) or not isinstance(value, int):
                    errors[name] = "Expected an integer"
                else:
                    values[name] = value
            elif isinstance(field, DateTimeField):
                try:
                    values[name] = datetime.fromisoformat(value) if isinstance(value, str) else value
                    if not isinstance(values[name], datetime):
                        raise ValueError
                except ValueError:
                    errors[name] = "Expected an ISO 8601 datetime"
            elif isinstance(field, ForeignKey):
                target_pk = field.to._meta.fields[field.to._meta.pk_name]
                if isinstance(target_pk, IntegerField):
                    valid = isinstance(value, int) and not isinstance(value, bool)
                else:
                    valid = isinstance(value, str)
                if not valid:
                    errors[name] = "Expected a related primary key"
                else:
                    values[name] = value
        if errors:
            raise ValidationError(errors)
        return values

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """DRF-style object-level hook; may be overridden with async def."""
        return attrs

    def _run_sync_hooks(self, values: dict[str, Any]) -> dict[str, Any]:
        errors: dict[str, Any] = {}
        for name, value in list(values.items()):
            hook = getattr(self, f"validate_{name}", None)
            if hook is None or inspect.iscoroutinefunction(hook):
                continue
            try:
                values[name] = hook(value)
            except ValidationError as exc:
                errors[name] = exc.errors
        if errors:
            raise ValidationError(errors)
        if not inspect.iscoroutinefunction(self.validate):
            values = self.validate(values)
        return values

    def is_valid(self, *, raise_exception: bool = False) -> bool:
        self.errors = {}
        try:
            values = self.to_internal_value(self.initial_data)
            values = self._run_sync_hooks(values)
            if not isinstance(values, dict):
                raise TypeError("validate() must return a dictionary")
            self.validated_data = values
        except ValidationError as exc:
            self.errors = exc.errors
            self.validated_data = None
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
        for name, nested in self._nested_serializers.items():
            if not await nested.ais_valid():
                self.errors[name] = nested.errors
            else:
                self.validated_data[name] = nested.validated_data
        for name, value in list(self.validated_data.items()):
            hook = getattr(self, f"validate_{name}", None)
            if hook is not None and inspect.iscoroutinefunction(hook) and name not in self.errors:
                try:
                    self.validated_data[name] = await hook(value)
                except ValidationError as exc:
                    self.errors[name] = exc.errors
        for name, value in list(self.validated_data.items()):
            if name in self.errors:
                continue
            field = model._meta.fields[name]
            if isinstance(field, ForeignKey) and value is not None and name not in self._nested_serializers:
                if not await field.to.objects.filter(pk=value).exists():
                    self.errors[name] = "Related object does not exist"
                    continue
            if not field.unique or value is None or name in self._nested_serializers:
                continue
            query = model.objects.filter(**{name: value})
            if self.instance is not None and self.instance.pk is not None:
                query = query.exclude(pk=self.instance.pk)
            if await query.exists():
                self.errors[name] = "A row with this value already exists"
        if not self.errors and inspect.iscoroutinefunction(self.validate):
            try:
                self.validated_data = await self.validate(self.validated_data)
            except ValidationError as exc:
                if isinstance(exc.errors, dict):
                    self.errors.update(exc.errors)
                else:
                    self.errors["non_field_errors"] = exc.errors
        if self.errors:
            self.validated_data = None
            if raise_exception:
                raise ValidationError(self.errors)
            return False
        return True

    async def create(self, validated_data: dict[str, Any]) -> Model:
        """Override this async hook for explicit writable nested creation."""
        nested = set(validated_data) & set(self._nested_serializers)
        if nested:
            raise ValidationError({name: "Write nested fields in an explicit create() hook"
                                   for name in nested})
        return await self.Meta.model.objects.create(**validated_data)

    async def update(self, instance: Model, validated_data: dict[str, Any]) -> Model:
        """Override this async hook for explicit writable nested updates."""
        nested = set(validated_data) & set(self._nested_serializers)
        if nested:
            raise ValidationError({name: "Write nested fields in an explicit update() hook"
                                   for name in nested})
        for name, value in validated_data.items():
            setattr(instance, name, value)
        await instance.save()
        return instance

    async def asave(self) -> Model:
        if self.validated_data is None:
            raise RuntimeError("Call is_valid() before asave()")
        try:
            async with transaction.atomic():
                if self.instance is None:
                    self.instance = await self.create(dict(self.validated_data))
                else:
                    self.instance = await self.update(self.instance,
                                                      dict(self.validated_data))
        except asyncpg.UniqueViolationError as exc:
            raise ValidationError({"non_field_errors": "Unique constraint violated"}) from exc
        return self.instance
