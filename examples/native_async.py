"""Exercise native async PostgreSQL calls without creating database objects."""

import asyncio
import os
from time import perf_counter

from asyncdjango.db import AsyncPostgres


async def main() -> None:
    dsn = os.environ.get("ASYNCDJANGO_DATABASE_URL")
    if not dsn:
        raise SystemExit("Set ASYNCDJANGO_DATABASE_URL to a PostgreSQL DSN")

    database = AsyncPostgres(dsn, max_size=5)
    await database.connect()
    try:
        started = perf_counter()
        results = await asyncio.gather(
            *(database.fetch("SELECT pg_sleep(0.1), $1::int AS n", n) for n in range(5))
        )
        elapsed = perf_counter() - started
        print([rows[0]["n"] for rows in results])
        print(f"Five queries completed in {elapsed:.3f}s")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
