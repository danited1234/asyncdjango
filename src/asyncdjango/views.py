"""Health and metrics endpoints for a worker's pools and event loop.

    from asyncdjango.views import health, metrics

    urlpatterns = [
        path("health/", health),                      # JSON, 503 when unhealthy
        path("health/", health, {"timeout": 2.0}),    # longer database check
        path("metrics/", metrics),                    # Prometheus text format
    ]

Each worker process has its own pools, so both report the worker that served
the request. Keep them off the public internet: they expose pool sizes and
error classes.
"""

from __future__ import annotations

import asyncio
import logging
import os

from django.http import HttpRequest, HttpResponse, JsonResponse

from .db.pool import get_pool, native_aliases, pool_stats
from .monitoring import stall_stats

logger = logging.getLogger("asyncdjango.health")

# psycopg_pool's instantaneous measures; every other statistic is a counter.
_GAUGES = {"pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting"}


async def _check(alias: str, timeout: float) -> str:
    try:
        pool = await get_pool(alias)
        async with pool.connection(timeout=timeout) as connection:
            await asyncio.wait_for(connection.execute("SELECT 1"), timeout)
    except Exception as exc:
        logger.warning("Health check of database %r failed", alias, exc_info=True)
        return type(exc).__name__
    return "ok"


async def health(request: HttpRequest, timeout: float = 1.0) -> JsonResponse:
    """Check out a connection from each pool and run ``SELECT 1``.

    Answers 503 when a database is unreachable or every pooled connection
    stays busy for ``timeout`` seconds.
    """
    aliases = native_aliases()
    results = await asyncio.gather(*(_check(alias, timeout) for alias in aliases))
    databases = dict(zip(aliases, results, strict=True))
    healthy = all(result == "ok" for result in results)
    return JsonResponse({
        "status": "ok" if healthy else "error",
        "databases": databases,
        "pools": pool_stats(),
        "stalls": stall_stats(),
    }, status=200 if healthy else 503)


async def metrics(request: HttpRequest) -> HttpResponse:
    """Pool and stall statistics in Prometheus' text exposition format."""
    worker = f'worker="{os.getpid()}"'
    samples: dict[str, tuple[str, list[str]]] = {}

    def add(name: str, kind: str, labels: str, value: float) -> None:
        samples.setdefault(name, (kind, []))[1].append(f"{name}{{{labels}}} {value}")

    for alias, stats in sorted(pool_stats().items()):
        labels = f'{worker},alias="{alias}"'
        for key, value in sorted(stats.items()):
            name = "asyncdjango_" + (key if key.startswith("pool_") else f"pool_{key}")
            if key in _GAUGES:
                add(name, "gauge", labels, value)
            else:
                add(f"{name}_total", "counter", labels, value)
    stalls = stall_stats()
    if stalls is not None:
        add("asyncdjango_loop_stalls_total", "counter", worker, stalls["stalls"])
        add("asyncdjango_loop_stall_seconds_total", "counter", worker, stalls["total"])
        add("asyncdjango_loop_stall_longest_seconds", "gauge", worker, stalls["longest"])
    lines = []
    for name, (kind, values) in samples.items():
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(values)
    return HttpResponse("\n".join(lines) + "\n",
                        content_type="text/plain; version=0.0.4; charset=utf-8")
