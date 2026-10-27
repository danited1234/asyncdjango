"""Run every stack through every scenario and save the results.

    python -m benchmarks.run [--concurrency 10 25 50 100] [--duration 8] [--repeat 3]
                             [--stacks ...] [--scenarios ...] [--output FILE]
    python -m benchmarks.plot FILE           # charts for the README

Each case runs in a fresh process: installing asyncdjango patches Django's
ORM globally, so the baselines must not load it. Results depend heavily on
the machine and database; compare stacks from the same run only.

With --repeat, the whole matrix runs that many times, round after round, so a
slow stretch on the machine affects every stack rather than one. Each case
reports its median round by throughput; every round is kept under "runs".
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from benchmarks.stacks import STACKS
from benchmarks.worker import SCENARIOS

COLUMNS = ["scenario", "concurrency", "stack", "rps", "p50_ms", "p95_ms", "p99_ms",
           "cpu_ms_per_request", "peak_threads", "peak_db_connections", "errors",
           "fingerprint"]


def worker(*args: str) -> str:
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    completed = subprocess.run([sys.executable, "-m", "benchmarks.worker", *args],
                               capture_output=True, text=True, env=env, check=False)
    if completed.returncode:
        sys.exit(completed.stderr)
    return completed.stdout


def environment() -> dict:
    import psycopg

    from benchmarks.settings import url

    packages = ["Django", "djangorestframework", "adrf", "asyncdjango", "psycopg",
                "psycopg-pool", "asgiref"]
    with psycopg.connect(f"{url.scheme}://{url.netloc}/postgres") as connection:
        server = connection.execute("SHOW server_version").fetchone()[0]
    return {
        "date": datetime.date.today().isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "postgresql": server.split()[0],
        "packages": {name: metadata.version(name) for name in packages},
    }


def medians(runs: list[dict], cases: list[tuple]) -> list[dict]:
    """The median-throughput round of each case (the lower one for an even count)."""
    rows = []
    for case in cases:
        rounds = sorted((row for row in runs
                         if (row["scenario"], row["concurrency"], row["stack"]) == case),
                        key=lambda row: row["rps"])
        if rounds:
            rows.append(rounds[(len(rounds) - 1) // 2])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, nargs="+", default=[10, 25, 50, 100])
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--repeat", type=int, default=1,
                        help="rounds of the whole matrix; each case reports its median")
    parser.add_argument("--stacks", nargs="+", default=list(STACKS), choices=list(STACKS))
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS),
                        choices=SCENARIOS)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/latest.json"))
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    worker("seed", "--stack", "drf")
    results = {"environment": environment(), "duration": args.duration,
               "pool_size": int(os.environ.get("BENCH_POOL", 20)), "repeat": args.repeat,
               "results": [], "runs": []}
    cases = [(scenario, concurrency, stack) for scenario in args.scenarios
             for concurrency in args.concurrency for stack in args.stacks]
    for round_number in range(1, args.repeat + 1):
        for scenario, concurrency, stack in cases:
            output = worker("run", "--stack", stack, "--scenario", scenario,
                            "--concurrency", str(concurrency),
                            "--duration", str(args.duration), "--warmup", str(args.warmup))
            row = {**json.loads(output.strip().splitlines()[-1]), "round": round_number}
            results["runs"].append(row)
            results["results"] = medians(results["runs"], cases)
            print(json.dumps(row), flush=True)
            # Saved after every case so an interrupted run keeps its rows.
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
