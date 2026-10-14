"""OpenAPI 3.1 schema generation for registered HTTP and model viewset routes."""

from __future__ import annotations

import re
from typing import Any

from .asgi import ASGIApplication
from .db.models import AutoField, BooleanField, CharField, DateTimeField, ForeignKey, IntegerField


def _field_schema(field: Any) -> dict[str, Any]:
    if isinstance(field, ForeignKey):
        field = field.to._meta.fields[field.to._meta.pk_name]
    if isinstance(field, CharField):
        schema = {"type": "string", "maxLength": field.max_length}
    elif isinstance(field, DateTimeField):
        schema = {"type": "string", "format": "date-time"}
    elif isinstance(field, BooleanField):
        schema = {"type": "boolean"}
    elif isinstance(field, IntegerField):
        schema = {"type": "integer"}
    else:
        schema = {"type": "string"}
    if field.null:
        schema = {"anyOf": [schema, {"type": "null"}]}
    if isinstance(field, AutoField):
        schema["readOnly"] = True
    return schema


def generate_openapi(application: ASGIApplication, *, title: str = "Asyncdjango API",
                     version: str = "0.0.1") -> dict[str, Any]:
    """Describe registered routes; model viewsets include field-level schemas."""
    paths: dict[str, dict[str, Any]] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for method, path, _, handler, _ in application._routes:
        action = getattr(handler, "__asyncdjango_action__", None)
        viewset = getattr(handler, "__asyncdjango_viewset__", None)
        operation: dict[str, Any] = {
            "operationId": f"{method.lower()}_{re.sub('[^a-zA-Z0-9]+', '_', path).strip('_')}",
            "responses": {"200": {"description": "Success"}},
        }
        parameter_names = re.findall(r"\{([a-zA-Z_]\w*)\}", path)
        if parameter_names:
            operation["parameters"] = [
                {"name": name, "in": "path", "required": True,
                 "schema": {"type": "string"}} for name in parameter_names
            ]
        if viewset is not None:
            serializer = viewset.serializer_class
            model = serializer.Meta.model
            schema_name = serializer.__name__
            properties = {name: _field_schema(model._meta.fields[name])
                          for name in serializer.Meta.fields}
            schemas[schema_name] = {"type": "object", "properties": properties}
            reference = {"$ref": f"#/components/schemas/{schema_name}"}
            if parameter_names and model._meta.pk_name in model._meta.fields:
                operation["parameters"][0]["schema"] = _field_schema(
                    model._meta.fields[model._meta.pk_name]
                )
            if action in ("create", "update"):
                write_properties = {name: schema for name, schema in properties.items()
                                    if not isinstance(model._meta.fields[name], AutoField)}
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": write_properties,
                    }}},
                }
            if action == "destroy":
                operation["responses"] = {"204": {"description": "Deleted"}}
            else:
                response_schema = {"type": "array", "items": reference} if action == "list" else reference
                status = "201" if action == "create" else "200"
                operation["responses"] = {status: {
                    "description": "Success",
                    "content": {"application/json": {"schema": response_schema}},
                }}
        paths.setdefault(path, {})[method.lower()] = operation
    return {"openapi": "3.1.0", "info": {"title": title, "version": version},
            "paths": paths, "components": {"schemas": schemas}}
