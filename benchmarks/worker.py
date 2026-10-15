"""One benchmark run in a fresh process (see benchmarks/run.py)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import threading
import time


def setup_django(mode: str) -> None:
    os.environ["BENCH_MODE"] = mode
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")
    import django

    django.setup()


def seed() -> None:
    """Recreate the benchmark database, migrate, and load fixed data."""
    import psycopg
    from django.core.management import call_command
    from django.db import connection

    from tests.testapp.models import Employee, Organization

    database = connection.settings_dict
    with psycopg.connect(dbname="postgres", user=database["USER"],
                         password=database["PASSWORD"], host=database["HOST"],
                         port=database["PORT"], autocommit=True) as admin:
        name = psycopg.sql.Identifier(database["NAME"])
        admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(name))
    call_command("migrate", verbosity=0)
    organizations = Organization.objects.bulk_create(
        Organization(name=f"org-{number:02d}") for number in range(50))
    Employee.objects.bulk_create(
        Employee(organization=org, email=f"e{org.pk}-{number}@example.com",
                 salary=1000 + number)
        for org in organizations for number in range(40)
    )


async def run(mode: str, scenario: str, concurrency: int, requests: int) -> dict:
    import psycopg
    from django.conf import settings
    from django.db import connections

    from asyncdjango.test import ASGIClient

    if mode == "native":
        from asyncdjango.asgi import NativeASGIHandler

        app = NativeASGIHandler()
    else:
        from django.core.handlers.asgi import ASGIHandler

        app = ASGIHandler()
    client = ASGIClient(app)
    paths = ([f"/orgs/{pk}/" for pk in range(1, 51)] if scenario == "detail" else ["/slow/"])

    # Correctness fingerprint: identical bodies in both modes.
    digest = hashlib.sha256()
    for path in paths:
        response = await client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.content)
        digest.update(response.content)

    database = settings.DATABASES["default"]
    sampler = await psycopg.AsyncConnection.connect(
        dbname=database["NAME"], user=database["USER"], password=database["PASSWORD"],
        host=database["HOST"], port=database["PORT"], autocommit=True)
    peak = {"threads": threading.active_count(), "connections": 0}
    stop = asyncio.Event()

    async def sample() -> None:
        while not stop.is_set():
            peak["threads"] = max(peak["threads"], threading.active_count())
            cursor = await sampler.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                [database["NAME"]])
            peak["connections"] = max(peak["connections"], (await cursor.fetchone())[0])
            await asyncio.sleep(0.01)

    latencies: list[float] = []
    errors: list[str] = []
    queue = list(range(requests))

    async def worker() -> None:
        while queue:
            number = queue.pop()
            started = time.perf_counter()
            response = await client.get(paths[number % len(paths)])
            latencies.append(time.perf_counter() - started)
            if response.status_code != 200:
                errors.append(f"{response.status_code}: {response.content[:300]!r}")

    sampler_task = asyncio.create_task(sample())
    cpu_started, wall_started = time.process_time(), time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall = time.perf_counter() - wall_started
    cpu = time.process_time() - cpu_started
    stop.set()
    await sampler_task
    await sampler.close()
    latencies.sort()

    def pct(p: float) -> float:
        return round(latencies[min(len(latencies) - 1, int(p * len(latencies)))] * 1000, 1)

    return {
        "mode": mode, "scenario": scenario, "concurrency": concurrency,
        "requests": requests, "rps": round(requests / wall, 1),
        "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
        "mean_ms": round(statistics.fmean(latencies) * 1000, 1),
        "cpu_ms_per_request": round(cpu / requests * 1000, 3),
        "peak_threads": peak["threads"], "peak_db_connections": peak["connections"],
        "fingerprint": digest.hexdigest()[:16],
        "errors": len(errors), "first_error": errors[0] if errors else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "run"])
    parser.add_argument("--mode", default="native", choices=["native", "adrf"])
    parser.add_argument("--scenario", default="detail", choices=["detail", "slow"])
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--requests", type=int, default=2000)
    args = parser.parse_args()
    os.environ["BENCH_POOL"] = str(args.concurrency)
    setup_django(args.mode)
    if args.command == "seed":
        seed()
        return
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    result = asyncio.run(run(args.mode, args.scenario, args.concurrency, args.requests),
                         loop_factory=loop_factory)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
