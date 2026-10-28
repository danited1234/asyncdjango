"""Benchmark every stack behind a real HTTP server, driven by wrk (Linux).

    python -m benchmarks.server [--concurrency 25 50 100] [--duration 10] [--repeat 3]
                                [--stacks ...] [--scenarios ...] [--output FILE]
                                [--server-cpus 0,1] [--client-cpus 2,3]

Unlike benchmarks/run.py, which calls each application in-process, this starts
the stack in its production shape (one uvicorn worker with uvloop and
httptools for ASGI stacks; one gunicorn gthread worker with `concurrency`
threads for the WSGI stack) and measures it over loopback HTTP with wrk. The
server and wrk are pinned to separate CPUs with taskset; run PostgreSQL on
others (benchmarks/docker/compose.yaml does this).

Results use run.py's format, so benchmarks/plot.py charts them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from importlib import metadata
from pathlib import Path

import psutil

from benchmarks.run import COLUMNS, environment, medians, worker
from benchmarks.stacks import STACKS
from benchmarks.worker import SCENARIOS, fingerprint_part, fingerprint_requests

PORT = 8765


def server_command(stack: str, concurrency: int) -> list[str]:
    address = f"127.0.0.1:{PORT}"
    if STACKS[stack].interface == "wsgi":
        return ["gunicorn", "benchmarks.wsgi:application", "--worker-class", "gthread",
                "--workers", "1", "--threads", str(concurrency), "--bind", address,
                "--log-level", "warning"]
    return ["uvicorn", "benchmarks.asgi:application", "--host", "127.0.0.1",
            "--port", str(PORT), "--workers", "1", "--loop", "uvloop", "--http", "httptools",
            "--no-access-log", "--log-level", "warning"]


def request(method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=body or None,
                                 method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_ready(process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            sys.exit(f"server exited with {process.returncode}")
        try:
            if request("GET", "/orgs/1/")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    sys.exit("server did not become ready")


def fingerprint(scenario: str) -> str:
    digest = hashlib.sha256()
    for method, path, body in fingerprint_requests(scenario):
        status, content = request(method, path, body)
        if status not in (200, 201):
            sys.exit(f"{method} {path} returned {status}: {content[:500]!r}")
        digest.update(fingerprint_part(scenario, status, content))
    return digest.hexdigest()[:16]


def process_tree(pid: int) -> list[psutil.Process]:
    root = psutil.Process(pid)
    return [root, *root.children(recursive=True)]


def cpu_seconds(pid: int) -> float:
    total = 0.0
    for process in process_tree(pid):
        try:
            times = process.cpu_times()
        except psutil.NoSuchProcess:
            continue
        total += times.user + times.system
    return total


class Sampler(threading.Thread):
    """Peak threads of the server's processes and peak database connections."""

    def __init__(self, pid: int) -> None:
        super().__init__(daemon=True)
        import psycopg

        from benchmarks.settings import url

        self.pid = pid
        self.database = url.path.lstrip("/")
        self.connection = psycopg.connect(f"{url.scheme}://{url.netloc}/postgres",
                                          autocommit=True)
        self.stopping = threading.Event()
        self.threads = 0
        self.connections = 0

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                threads = sum(process.num_threads() for process in process_tree(self.pid))
            except psutil.NoSuchProcess:
                threads = 0
            self.threads = max(self.threads, threads)
            count = self.connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s",
                [self.database]).fetchone()[0]
            self.connections = max(self.connections, count)
            self.stopping.wait(0.05)

    def stop(self) -> None:
        self.stopping.set()
        self.join()
        self.connection.close()


def wrk(scenario: str, concurrency: int, duration: float, cpus: str) -> dict:
    threads = min(len(cpus.split(",")), concurrency)
    command = ["taskset", "-c", cpus, "wrk", "--threads", str(threads),
               "--connections", str(concurrency), "--duration", f"{duration:g}s",
               "--timeout", "10s", "--script", str(Path(__file__).with_name("wrk.lua")),
               f"http://127.0.0.1:{PORT}"]
    env = {**os.environ, "BENCH_SCENARIO": scenario, "BENCH_RUN": uuid.uuid4().hex[:8]}
    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    for line in completed.stdout.splitlines():
        if line.startswith("BENCH_JSON "):
            return json.loads(line.removeprefix("BENCH_JSON "))
    sys.exit(f"wrk failed: {completed.stdout}\n{completed.stderr}")


def run_case(stack: str, scenario: str, concurrency: int, duration: float, warmup: float,
             server_cpus: str, client_cpus: str) -> dict:
    env = {**os.environ, "BENCH_STACK": stack, "PYTHONPATH": os.getcwd()}
    process = subprocess.Popen(["taskset", "-c", server_cpus,
                                *server_command(stack, concurrency)], env=env)
    try:
        wait_ready(process)
        digest = fingerprint(scenario)
        wrk(scenario, concurrency, warmup, client_cpus)
        sampler = Sampler(process.pid)
        sampler.start()
        cpu_before = cpu_seconds(process.pid)
        result = wrk(scenario, concurrency, duration, client_cpus)
        cpu = cpu_seconds(process.pid) - cpu_before
        sampler.stop()
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    count = max(result["requests"], 1)
    errors = result["socket_errors"] + result["status_errors"]
    return {
        "stack": stack, "scenario": scenario, "concurrency": concurrency,
        "server": "gunicorn gthread" if STACKS[stack].interface == "wsgi" else "uvicorn",
        "requests": result["requests"],
        "rps": round(result["requests"] / (result["duration_us"] / 1e6), 1),
        "p50_ms": round(result["p50_us"] / 1000, 1),
        "p95_ms": round(result["p95_us"] / 1000, 1),
        "p99_ms": round(result["p99_us"] / 1000, 1),
        "mean_ms": round(result["mean_us"] / 1000, 1),
        "cpu_ms_per_request": round(cpu / count * 1000, 2),
        "peak_threads": sampler.threads, "peak_db_connections": sampler.connections,
        "fingerprint": digest, "errors": errors,
        "first_error": f"{errors} wrk errors" if errors else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, nargs="+", default=[25, 50, 100])
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("--repeat", type=int, default=1,
                        help="rounds of the whole matrix; each case reports its median")
    parser.add_argument("--stacks", nargs="+", default=list(STACKS), choices=list(STACKS))
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS),
                        choices=SCENARIOS)
    parser.add_argument("--server-cpus", default="0,1")
    parser.add_argument("--client-cpus", default="2,3")
    parser.add_argument("--output", type=Path,
                        default=Path("benchmarks/results/server-latest.json"))
    args = parser.parse_args()
    for tool in ("wrk", "taskset"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is required (Linux); see docs/benchmarks.md")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    worker("seed", "--stack", "drf")
    wrk_version = subprocess.run(["wrk", "--version"], capture_output=True,
                                 text=True).stdout.split("\n")[0]
    env = environment()
    env["packages"].update({name: metadata.version(name)
                            for name in ("uvicorn", "uvloop", "httptools", "gunicorn")})
    env.update({"load_generator": wrk_version.strip(), "server_cpus": args.server_cpus,
                "client_cpus": args.client_cpus, "mode": "server"})
    results = {"environment": env, "duration": args.duration,
               "pool_size": int(os.environ.get("BENCH_POOL", 20)), "repeat": args.repeat,
               "results": [], "runs": []}
    cases = [(scenario, concurrency, stack) for scenario in args.scenarios
             for concurrency in args.concurrency for stack in args.stacks]
    for round_number in range(1, args.repeat + 1):
        for scenario, concurrency, stack in cases:
            row = {**run_case(stack, scenario, concurrency, args.duration, args.warmup,
                              args.server_cpus, args.client_cpus),
                   "round": round_number}
            results["runs"].append(row)
            results["results"] = medians(results["runs"], cases)
            print(json.dumps(row), flush=True)
            args.output.write_text(json.dumps(results, indent=2) + "\n")

    print(f"\nSaved {args.output}\n")
    print("| " + " | ".join(COLUMNS) + " |")
    print("|" + "---|" * len(COLUMNS))
    for row in results["results"]:
        print("| " + " | ".join(str(row[column]) for column in COLUMNS) + " |")
    for scenario in args.scenarios:
        fingerprints = {row["fingerprint"] for row in results["runs"]
                        if row["scenario"] == scenario}
        if len(fingerprints) != 1:
            sys.exit(f"Responses differ between stacks for {scenario}: {fingerprints}")


if __name__ == "__main__":
    main()
