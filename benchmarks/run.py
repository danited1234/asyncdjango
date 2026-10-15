"""Compare ADRF (Django's ASGIHandler) with asyncdjango on one PostgreSQL workload.

    python -m benchmarks.run [--requests 2000] [--concurrency 25 50]

Each mode runs in its own process: installing asyncdjango patches Django's
ORM globally, so the ADRF baseline must not load it. Results depend heavily on
the machine and database; compare modes from the same run only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

COLUMNS = ["scenario", "concurrency", "mode", "rps", "p50_ms", "p95_ms", "p99_ms",
           "cpu_ms_per_request", "peak_threads", "peak_db_connections", "errors",
           "fingerprint"]


def worker(*args: str) -> str:
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    completed = subprocess.run([sys.executable, "-m", "benchmarks.worker", *args],
                               capture_output=True, text=True, env=env, check=False)
    if completed.returncode:
        sys.exit(completed.stderr)
    return completed.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[25, 50])
    parser.add_argument("--scenarios", nargs="+", default=["detail", "slow"])
    args = parser.parse_args()
    worker("seed", "--mode", "adrf")
    rows = []
    for scenario in args.scenarios:
        for concurrency in args.concurrency:
            for mode in ("adrf", "native"):
                output = worker("run", "--mode", mode, "--scenario", scenario,
                                "--concurrency", str(concurrency),
                                "--requests", str(args.requests))
                rows.append(json.loads(output.strip().splitlines()[-1]))
                print(json.dumps(rows[-1]), flush=True)
    print()
    print("| " + " | ".join(COLUMNS) + " |")
    print("|" + "---|" * len(COLUMNS))
    for row in rows:
        print("| " + " | ".join(str(row[column]) for column in COLUMNS) + " |")


if __name__ == "__main__":
    main()
