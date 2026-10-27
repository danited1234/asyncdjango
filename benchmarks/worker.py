"""One benchmark case in a fresh process (see benchmarks/run.py).

ASGI stacks are driven in-process through asyncdjango.test.ASGIClient by
`concurrency` asyncio tasks. The WSGI stack is driven by `concurrency` threads
calling Django's WSGIHandler, the way a threaded WSGI server (gunicorn
gthread, mod_wsgi) runs it. No HTTP server or network stack is measured.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import statistics
import sys
import threading
import time
import uuid

SCENARIOS = ("read", "write", "slow")
READ_ORGANIZATIONS = range(1, 51)  # never written to, so read bodies stay fixed
WRITE_ORGANIZATIONS = range(51, 61)


def setup_django(stack: str) -> None:
    os.environ["BENCH_STACK"] = stack
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")
    import django

    django.setup()


def seed() -> None:
    """Recreate the benchmark database, migrate, and load fixed data."""
    import psycopg
    from django.core.management import call_command
    from django.db import connection

    from benchmarks.bench.models import Employee, Organization

    database = connection.settings_dict
    with psycopg.connect(dbname="postgres", user=database["USER"],
                         password=database["PASSWORD"], host=database["HOST"],
                         port=database["PORT"], autocommit=True) as admin:
        name = psycopg.sql.Identifier(database["NAME"])
        admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(name))
    call_command("migrate", verbosity=0)
    organizations = Organization.objects.bulk_create(
        Organization(name=f"org-{number:02d}") for number in range(1, 61))
    Employee.objects.bulk_create(
        Employee(organization=org, email=f"e{org.pk}-{number}@example.com",
                 salary=1000 + number)
        for org in organizations for number in range(40)
    )


def make_request(scenario: str, number: int) -> tuple[str, str, bytes]:
    if scenario == "read":
        pk = READ_ORGANIZATIONS[number % len(READ_ORGANIZATIONS)]
        return "GET", f"/orgs/{pk}/", b""
    if scenario == "write":
        pk = WRITE_ORGANIZATIONS[number % len(WRITE_ORGANIZATIONS)]
        body = json.dumps({"email": f"w-{uuid.uuid4().hex}@example.com",
                           "salary": "1234.50"}).encode()
        return "POST", f"/orgs/{pk}/employees/", body
    return "GET", "/slow/", b""


def fingerprint_part(scenario: str, status: int, content: bytes) -> bytes:
    if scenario == "write":
        # New ids and emails differ per run; compare the status and the shape.
        body = json.loads(content)
        shape = {key: sorted(value) if isinstance(value, dict) else type(value).__name__
                 for key, value in body.items()}
        return f"{status}:{json.dumps(shape, sort_keys=True)}".encode()
    return f"{status}:".encode() + content


def fingerprint_requests(scenario: str) -> list[tuple[str, str, bytes]]:
    count = len(READ_ORGANIZATIONS) if scenario == "read" else 1
    return [make_request(scenario, number) for number in range(count)]


class Sampler(threading.Thread):
    """Peak threads (excluding this one) and peak database connections."""

    def __init__(self, database: dict) -> None:
        super().__init__(daemon=True)
        import psycopg

        self.connection = psycopg.connect(
            dbname=database["NAME"], user=database["USER"], password=database["PASSWORD"],
            host=database["HOST"], port=database["PORT"], autocommit=True)
        self.name_ = database["NAME"]
        self.stopping = threading.Event()
        self.threads = 0
        self.connections = 0

    def run(self) -> None:
        while not self.stopping.is_set():
            self.threads = max(self.threads, threading.active_count() - 1)
            count = self.connection.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", [self.name_]).fetchone()[0]
            self.connections = max(self.connections, count)
            self.stopping.wait(0.05)

    def stop(self) -> None:
        self.stopping.set()
        self.join()
        self.connection.close()


def summarize(stack, scenario, concurrency, latencies, errors, wall, cpu, sampler,
              digest) -> dict:
    latencies.sort()
    count = len(latencies)

    def pct(p: float) -> float:
        return round(latencies[min(count - 1, int(p * count))] * 1000, 1)

    return {
        "stack": stack, "scenario": scenario, "concurrency": concurrency,
        "requests": count, "rps": round(count / wall, 1),
        "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
        "mean_ms": round(statistics.fmean(latencies) * 1000, 1),
        "cpu_ms_per_request": round(cpu / count * 1000, 2),
        "peak_threads": sampler.threads, "peak_db_connections": sampler.connections,
        "fingerprint": digest.hexdigest()[:16],
        "errors": len(errors), "first_error": errors[0] if errors else None,
    }


async def run_asgi(stack, scenario, concurrency, duration, warmup) -> dict:
    from django.conf import settings

    from asyncdjango.test import ASGIClient

    if settings.STACK.native:
        from asyncdjango.asgi import NativeASGIHandler

        app = NativeASGIHandler()
    else:
        from django.core.handlers.asgi import ASGIHandler

        app = ASGIHandler()
    client = ASGIClient(app)
    headers = {"content-type": "application/json"}

    async def call(method, path, body):
        response = await client.request(method, path, body=body, headers=headers)
        return response.status_code, response.content

    digest = hashlib.sha256()
    for request in fingerprint_requests(scenario):
        status, content = await call(*request)
        assert status in (200, 201), (request, status, content[:500])
        digest.update(fingerprint_part(scenario, status, content))

    counter = iter(range(10**9))

    async def loop(until: float, latencies: list, errors: list) -> None:
        while time.perf_counter() < until:
            request = make_request(scenario, next(counter))
            started = time.perf_counter()
            status, content = await call(*request)
            latencies.append(time.perf_counter() - started)
            if status not in (200, 201):
                errors.append(f"{status}: {content[:300]!r}")

    until = time.perf_counter() + warmup
    await asyncio.gather(*(loop(until, [], []) for _ in range(concurrency)))

    latencies: list[float] = []
    errors: list[str] = []
    sampler = Sampler(settings.DATABASES["default"])
    sampler.start()
    cpu_started, wall_started = time.process_time(), time.perf_counter()
    until = wall_started + duration
    await asyncio.gather(*(loop(until, latencies, errors) for _ in range(concurrency)))
    wall = time.perf_counter() - wall_started
    cpu = time.process_time() - cpu_started
    sampler.stop()
    return summarize(stack, scenario, concurrency, latencies, errors, wall, cpu, sampler,
                     digest)


def run_wsgi(stack, scenario, concurrency, duration, warmup) -> dict:
    from django.conf import settings
    from django.core.handlers.wsgi import WSGIHandler

    app = WSGIHandler()

    def call(method, path, body):
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "SCRIPT_NAME": "",
            "QUERY_STRING": "", "SERVER_NAME": "testserver", "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_HOST": "testserver",
            "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1", "wsgi.input": io.BytesIO(body),
            "wsgi.errors": sys.stderr, "wsgi.url_scheme": "http", "wsgi.version": (1, 0),
            "wsgi.multithread": True, "wsgi.multiprocess": False, "wsgi.run_once": False,
        }
        started = []
        result = app(environ, lambda status, headers, exc_info=None: started.append(status))
        try:
            content = b"".join(result)
        finally:
            result.close()  # request_finished: returns the connection to the pool
        return int(started[0].split()[0]), content

    digest = hashlib.sha256()
    for request in fingerprint_requests(scenario):
        status, content = call(*request)
        assert status in (200, 201), (request, status, content[:500])
        digest.update(fingerprint_part(scenario, status, content))

    counter = iter(range(10**9))
    counter_lock = threading.Lock()

    def loop(until: float, latencies: list, errors: list) -> None:
        while time.perf_counter() < until:
            with counter_lock:
                number = next(counter)
            request = make_request(scenario, number)
            started = time.perf_counter()
            status, content = call(*request)
            latencies.append(time.perf_counter() - started)
            if status not in (200, 201):
                errors.append(f"{status}: {content[:300]!r}")

    def run_threads(until, latencies, errors):
        threads = [threading.Thread(target=loop, args=(until, latencies, errors))
                   for _ in range(concurrency)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    run_threads(time.perf_counter() + warmup, [], [])
    latencies: list[float] = []
    errors: list[str] = []
    sampler = Sampler(settings.DATABASES["default"])
    sampler.start()
    cpu_started, wall_started = time.process_time(), time.perf_counter()
    run_threads(wall_started + duration, latencies, errors)
    wall = time.perf_counter() - wall_started
    cpu = time.process_time() - cpu_started
    sampler.stop()
    return summarize(stack, scenario, concurrency, latencies, errors, wall, cpu, sampler,
                     digest)


def main() -> None:
    from benchmarks.stacks import STACKS

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "run"])
    parser.add_argument("--stack", default="asyncdjango-drf", choices=list(STACKS))
    parser.add_argument("--scenario", default="read", choices=SCENARIOS)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    args = parser.parse_args()
    setup_django(args.stack)
    if args.command == "seed":
        seed()
        return
    if STACKS[args.stack].interface == "wsgi":
        result = run_wsgi(args.stack, args.scenario, args.concurrency, args.duration,
                          args.warmup)
    else:
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        result = asyncio.run(run_asgi(args.stack, args.scenario, args.concurrency,
                                      args.duration, args.warmup),
                             loop_factory=loop_factory)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
