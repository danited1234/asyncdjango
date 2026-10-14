"""Explicitly allowlisted queryset filtering and ordering backends."""

from __future__ import annotations

from typing import Any

from .asgi import Request
from .db.models import QuerySet


class FilterError(ValueError):
    pass


class BaseFilterBackend:
    def filter_queryset(self, request: Request, queryset: QuerySet,
                        view: Any) -> QuerySet:
        raise NotImplementedError


class ExactFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request: Request, queryset: QuerySet,
                        view: Any) -> QuerySet:
        allowed = set(getattr(view, "filterset_fields", ()))
        for name in allowed & request.query_params.keys():
            if name not in queryset.model._meta.fields:
                raise FilterError(f"Unknown filter field: {name}")
            field = queryset.model._meta.fields[name]
            raw = request.query_params[name]
            from .db.models import BooleanField, IntegerField
            if isinstance(field, BooleanField):
                if raw.lower() not in ("true", "false"):
                    raise FilterError(f"Invalid boolean filter: {name}")
                value: Any = raw.lower() == "true"
            elif isinstance(field, IntegerField):
                try:
                    value = int(raw)
                except ValueError as exc:
                    raise FilterError(f"Invalid integer filter: {name}") from exc
            else:
                value = raw
            queryset = queryset.filter(**{name: value})
        return queryset


class OrderingFilter(BaseFilterBackend):
    def filter_queryset(self, request: Request, queryset: QuerySet,
                        view: Any) -> QuerySet:
        raw = request.query_params.get("ordering")
        if not raw:
            return queryset
        fields = tuple(item.strip() for item in raw.split(","))
        allowed = set(getattr(view, "ordering_fields", ()))
        if not all((field[1:] if field.startswith("-") else field) in allowed
                   and not field.startswith("--") for field in fields):
            raise FilterError("Unsupported ordering field")
        return queryset.order_by(*fields)
