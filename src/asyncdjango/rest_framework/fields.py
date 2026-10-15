from __future__ import annotations

from typing import Any

from rest_framework import fields as drf_fields

from ..db.scope import run_native
from ._compat import resolve


class SerializerMethodField(drf_fields.SerializerMethodField):
    """Accepts ``get_<name>`` methods written as ``def`` or ``async def``."""

    def to_representation(self, value: Any) -> Any:
        method = getattr(self.parent, self.method_name)
        return resolve(method(value))

    async def ato_representation(self, value: Any) -> Any:
        return await run_native(self.to_representation, value)
