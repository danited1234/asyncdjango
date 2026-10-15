"""Routers that map standard actions to ADRF's async names for async viewsets."""

from __future__ import annotations

from typing import Any

from rest_framework import routers as drf_routers

ASYNC_ACTION_MAP = {
    "list": "alist",
    "create": "acreate",
    "retrieve": "aretrieve",
    "update": "aupdate",
    "partial_update": "partial_aupdate",
    "destroy": "adestroy",
}


class SimpleRouter(drf_routers.SimpleRouter):
    def get_method_map(self, viewset: Any, method_map: dict[str, str]) -> dict[str, str]:
        if getattr(viewset, "view_is_async", False):
            method_map = {
                method: ASYNC_ACTION_MAP[action]
                if hasattr(viewset, ASYNC_ACTION_MAP.get(action, "")) else action
                for method, action in method_map.items()
            }
        return super().get_method_map(viewset, method_map)


class DefaultRouter(SimpleRouter, drf_routers.DefaultRouter):
    pass
