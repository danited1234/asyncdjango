# Benchmarks: ADRF versus asyncdjango

`benchmarks/` runs the same endpoint code on two stacks against the same
PostgreSQL data:

- **adrf**: Django's `ASGIHandler` with adrf 0.1.12 views and serializers,
  `CONN_MAX_AGE = 0` (the CRM's production setting).
- **native**: `asyncdjango.asgi.NativeASGIHandler` with
  `asyncdjango.rest_framework`, pool `max_size` equal to the concurrency.

Only the imports differ (`benchmarks/urls.py`). Each mode runs in its own
process because installing asyncdjango patches the ORM globally.

```console
python -m benchmarks.run --requests 1000 --concurrency 25 50
```

The runner recreates the `asyncdjango_bench` database (50 organizations, 2,000
employees). Before timing, each mode fetches every URL once and hashes the
response bodies; the `fingerprint` column must match between modes, and it
did in every run below. Requests are sent in-process through ASGI, so no HTTP
server or network stack is measured.

## Scenarios

- **detail** (`/orgs/<pk>/`): `aget` an organization, `acount` its employees,
  and serialize 20 of them with a nested serializer. This is a typical CRM
  read, and mostly Python CPU work.
- **slow** (`/slow/`): one query that sleeps 50 ms in PostgreSQL. This is a
  database-bound request.

## Results

Environment: Windows 10, Intel Core i5-8350U (4 cores, 8 threads, 1.7 GHz),
16 GB RAM, Python 3.13.12, Django 6.0.3, DRF 3.16.1, adrf 0.1.12, psycopg 3.3.2,
PostgreSQL 18.6 on the same machine, 2026-10-15. One process per mode.

| scenario | concurrency | mode | req/s | p50 ms | p95 ms | p99 ms | CPU ms/request | peak threads | peak DB connections |
|---|---|---|---|---|---|---|---|---|---|
| detail | 25 | adrf | 11.0 | 2115 | 3283 | 3379 | 109.9 | 27 | 25 |
| detail | 25 | native | 172.0 | 135 | 230 | 253 | 5.5 | 1 | 21 |
| detail | 50 | adrf | 9.6 | 5128 | 6629 | 6878 | 122.5 | 52 | 50 |
| detail | 50 | native | 76.9* | 618 | 948 | 1010 | 12.1 | 1 | 23 |
| slow | 25 | adrf | 31.1 | 786 | 1074 | 1157 | 30.9 | 27 | 18 |
| slow | 25 | native | 252.5 | 94 | 149 | 195 | 3.4 | 1 | 25 |
| slow | 50 | adrf | 33.5 | 1440 | 2144 | 2496 | 30.3 | 52 | 30 |
| slow | 50 | native | 306.1 | 146 | 277 | 406 | 2.7 | 1 | 36 |

\* An outlier. Two reruns of the same case right afterwards gave 146.0 and
158.2 req/s at 6.4 and 5.9 CPU ms per request (p50 318 and 304 ms).

After later correctness fixes (context-variable propagation across greenlet
switches), native detail measured 163.1 req/s at concurrency 25 and 165.6 at
50, both at 5.8 CPU ms per request.

## Reading the numbers

- **Where ADRF's time goes.** For the detail endpoint ADRF spends about 110 ms
  of CPU per request, compared with about 6 ms native. ADRF calls
  `sync_to_async` for `initial()`, for each ORM call, and for every field of
  every serialized row (`field.get_attribute` and `field.to_representation`).
  Counted with `ThreadOffloadDetector`, one detail request makes 267 thread
  hops under ADRF and none under asyncdjango. ADRF also opens a new database
  connection per request because `CONN_MAX_AGE` must be 0.
- **Threads.** ADRF needs one thread per in-flight request (`peak_threads`
  follows concurrency). asyncdjango ran everything on the event-loop thread.
- **Native saturates one core.** At about 170 req/s for the detail endpoint
  the process is CPU-bound in DRF serialization and Django model construction.
  Latency then grows with concurrency because requests queue for the CPU. Add
  worker processes to scale. For the database-bound scenario the native
  process stays far from CPU saturation.
- **Database connections.** Native is bounded by the pool. ADRF is bounded by
  thread count and opens and closes a connection for every request.

These figures come from one laptop. The ratios should carry over to Linux,
but measure your own endpoints before relying on them.
