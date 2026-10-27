# Benchmarks

`benchmarks/` runs the same three endpoints on five Django stacks against the
same PostgreSQL data, so you can compare asyncdjango with the stacks it
replaces.

| stack | handler | views and serializers |
|---|---|---|
| `asyncdjango-drf` | `asyncdjango.asgi.NativeASGIHandler` | `asyncdjango.rest_framework` |
| `adrf` | Django's `ASGIHandler` | [adrf](https://github.com/em1208/adrf) 0.1.12 |
| `drf` | Django's `WSGIHandler`, one thread per client | DRF 3.16, synchronous |
| `asyncdjango` | `asyncdjango.asgi.NativeASGIHandler` | plain Django async views |
| `django` | Django's `ASGIHandler` | plain Django async views |

The async DRF stacks share one views module (`benchmarks/views_async_drf.py`);
only the imports and the validation call differ. The plain Django stacks share
`benchmarks/views_django.py`. `benchmarks/views_drf.py` is the same API as
classic synchronous DRF. Every stack gets a connection pool of the same size
(`BENCH_POOL`, default 20): asyncdjango's own pool, or Django's built-in
psycopg pool (`OPTIONS["pool"]`, Django 5.1+) for the others. Authentication,
permissions, and middleware are off in every stack, so the numbers measure
the ORM, the serializers, and the request handler.

## Scenarios

- **read** (`GET /orgs/<pk>/`): `aget` an organization, `acount` its
  employees, and serialize 20 of them with a nested serializer. Mostly Python
  CPU work.
- **write** (`POST /orgs/<pk>/employees/`): validate a payload (including the
  unique-email query) and insert a row.
- **slow** (`GET /slow/`): one query that waits 50 ms in PostgreSQL
  (`pg_sleep(0.05)`). Database-bound.

## Running it

```console
pip install -e ".[drf,bench]" "psycopg[binary]"
python -m benchmarks.run --repeat 3               # every stack, scenario, concurrency
python -m benchmarks.run --stacks adrf asyncdjango-drf --scenarios read --concurrency 50
python -m benchmarks.plot benchmarks/results/latest.json --machine "your CPU"
```

`ASYNCDJANGO_BENCH_DATABASE_URL` selects the server (default
`postgresql://postgres:postgres@localhost:5432/asyncdjango_bench`). The runner
drops and recreates that database (60 organizations, 2,400 employees). Each
case runs in a fresh process for `--warmup` seconds, then is timed for
`--duration` seconds. Results are written to `benchmarks/results/latest.json`
after every case, together with the platform, CPU, Python, PostgreSQL, and
package versions.

Laptops and shared CI runners drift by 20% or more between runs (thermal
throttling, background work). `--repeat N` runs the whole matrix N times,
round after round, so a slow stretch hits every stack instead of one. Each
case then reports its median round by throughput, and every round is kept
under `runs` in the results file.

Before timing, every stack's response bodies are hashed. The `fingerprint`
must be identical across stacks for each scenario, and the runner exits with
an error if it isn't. This shows that every stack returned the same responses.

Requests are sent in-process: ASGI stacks through `asyncdjango.test.ASGIClient`
from `concurrency` asyncio tasks, the WSGI stack from `concurrency` threads
calling `WSGIHandler` (the way gunicorn's `gthread` worker runs it). No HTTP
server or network stack is measured, and each stack runs in one process.

## Results

Measured on 2026-10-27: Windows 10, Intel Core i5-8350U (4 cores, 8 threads,
1.7 GHz), 16 GB RAM, Python 3.13.12, Django 6.0.3, DRF 3.16.1, adrf 0.1.12,
psycopg 3.3.2, psycopg-pool 3.3.3, PostgreSQL 18.6 on the same machine.
`python -m benchmarks.run --repeat 3`: 8 s per case after a 2 s warm-up, a
20-connection pool, and the median of three rounds. The raw rounds are in
[`benchmarks/results/latest.json`](../benchmarks/results/latest.json).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/bench-throughput-dark.svg">
  <img alt="Requests per second at 50 concurrent clients for each stack and scenario" src="images/bench-throughput-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/bench-cpu-dark.svg">
  <img alt="CPU time per request at 50 concurrent clients for each stack and scenario" src="images/bench-cpu-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/bench-latency-dark.svg">
  <img alt="99th percentile latency at 50 concurrent clients for each stack and scenario" src="images/bench-latency-light.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/bench-scaling-dark.svg">
  <img alt="Requests per second from 10 to 100 concurrent clients, with the range of the three rounds" src="images/bench-scaling-light.svg">
</picture>

### Reading the numbers

- **asyncdjango.rest_framework versus ADRF.** With the same view and
  serializer code, the read endpoint served about 10 times the requests
  (199 against 19 req/s at 50 clients) at a twelfth of the CPU per request
  (5.0 against 58.5 ms). The write endpoint served twice as many. ADRF 0.1.12
  wraps every field of every serialized row in its own `sync_to_async` call
  (`field.get_attribute` and `field.to_representation`), so serializing 20
  rows makes about 200 thread hand-offs. asyncdjango makes none.
- **asyncdjango versus Django's async views.** With plain Django views and the
  same ORM calls, asyncdjango served about 30% more requests (264 against 198
  req/s for read, 302 against 233 for write, at 50 clients) at 35–40% less CPU
  per request. Django's async ORM methods run each query in a worker thread
  (`sync_to_async`); asyncdjango awaits it on the event loop.
- **asyncdjango.rest_framework versus synchronous DRF.** Classic DRF on a
  threaded WSGI server is about as fast on these endpoints (225 against 199
  req/s for read at 50 clients), because nothing in them waits on I/O for
  long. The difference is resources: DRF needs one thread per in-flight
  request (56 threads at 50 clients), asyncdjango one thread in total.
- **Database-bound requests.** In the `slow` scenario every stack is limited
  by the pool: 20 connections × one 50 ms query each is at most 400 req/s.
  All stacks land between 270 and 306 req/s; asyncdjango is highest and uses
  the least CPU, but the pool size matters more than the stack here.
- **Threads.** Every stack except asyncdjango needs a thread per concurrent
  request (`threads` column). asyncdjango runs every request on the event-loop
  thread.
- **Variance.** Rounds of the same case sometimes differ by half or more on this laptop
  (see the ranges below and the bands in the scaling chart). asyncdjango.rest_framework
  at 100 clients was the least stable case, with rounds between 106 and 212
  req/s for read and between 67 and 263 for write. Plain asyncdjango at 100
  clients stayed between 270 and 390. The cause is not known yet and is
  tracked in `todo.md`.

These figures come from one laptop, with the client and the database on the
same machine. Run the benchmark on your own hardware before relying on
them, and compare stacks only within one run.

### All results (medians of three rounds)

| scenario | clients | stack | req/s (median) | req/s range | p50 ms | p99 ms | CPU ms/request | threads | DB connections |
|---|---|---|---|---|---|---|---|---|---|
| read | 10 | asyncdjango-drf | 238 | 190–246 | 39 | 87 | 4.2 | 1 | 10 |
| read | 10 | adrf | 20 | 18–21 | 473 | 758 | 55.9 | 16 | 10 |
| read | 10 | drf | 187 | 134–209 | 44 | 156 | 5.6 | 15 | 11 |
| read | 10 | asyncdjango | 284 | 267–325 | 32 | 112 | 3.5 | 1 | 10 |
| read | 10 | django | 213 | 209–221 | 47 | 66 | 5.2 | 16 | 10 |
| read | 25 | asyncdjango-drf | 205 | 190–206 | 115 | 209 | 4.8 | 1 | 20 |
| read | 25 | adrf | 22 | 21–22 | 982 | 1839 | 52.3 | 31 | 20 |
| read | 25 | drf | 240 | 229–243 | 98 | 205 | 4.4 | 30 | 20 |
| read | 25 | asyncdjango | 310 | 205–313 | 78 | 123 | 3.2 | 1 | 20 |
| read | 25 | django | 220 | 166–224 | 113 | 142 | 5.0 | 33 | 20 |
| read | 50 | asyncdjango-drf | 199 | 190–206 | 228 | 481 | 5.0 | 1 | 20 |
| read | 50 | adrf | 19 | 16–22 | 2306 | 3327 | 58.5 | 56 | 20 |
| read | 50 | drf | 225 | 221–250 | 206 | 379 | 4.6 | 55 | 20 |
| read | 50 | asyncdjango | 264 | 241–300 | 163 | 707 | 3.5 | 1 | 20 |
| read | 50 | django | 198 | 100–206 | 249 | 351 | 5.7 | 56 | 20 |
| read | 100 | asyncdjango-drf | 161 | 106–212 | 519 | 1278 | 6.0 | 1 | 20 |
| read | 100 | adrf | 19 | 18–20 | 4870 | 6268 | 61.3 | 106 | 20 |
| read | 100 | drf | 222 | 193–236 | 418 | 725 | 4.8 | 105 | 20 |
| read | 100 | asyncdjango | 299 | 270–309 | 320 | 395 | 3.3 | 1 | 20 |
| read | 100 | django | 214 | 208–214 | 466 | 536 | 5.2 | 107 | 20 |
| write | 10 | asyncdjango-drf | 246 | 209–265 | 38 | 64 | 4.1 | 1 | 10 |
| write | 10 | adrf | 126 | 123–131 | 77 | 112 | 8.7 | 16 | 10 |
| write | 10 | drf | 335 | 295–337 | 28 | 57 | 3.3 | 15 | 10 |
| write | 10 | asyncdjango | 328 | 213–384 | 27 | 64 | 3.0 | 1 | 10 |
| write | 10 | django | 256 | 183–261 | 38 | 61 | 4.6 | 17 | 10 |
| write | 25 | asyncdjango-drf | 238 | 160–266 | 95 | 246 | 4.1 | 1 | 20 |
| write | 25 | adrf | 88 | 84–133 | 219 | 919 | 11.5 | 32 | 20 |
| write | 25 | drf | 302 | 241–321 | 78 | 161 | 3.6 | 30 | 21 |
| write | 25 | asyncdjango | 395 | 284–419 | 61 | 92 | 2.5 | 1 | 20 |
| write | 25 | django | 272 | 196–276 | 91 | 122 | 4.2 | 31 | 20 |
| write | 50 | asyncdjango-drf | 254 | 236–259 | 187 | 363 | 4.0 | 1 | 20 |
| write | 50 | adrf | 125 | 109–132 | 389 | 539 | 9.0 | 57 | 20 |
| write | 50 | drf | 300 | 255–311 | 157 | 283 | 3.6 | 55 | 20 |
| write | 50 | asyncdjango | 302 | 216–328 | 156 | 421 | 3.2 | 1 | 20 |
| write | 50 | django | 233 | 135–244 | 204 | 368 | 4.9 | 58 | 21 |
| write | 100 | asyncdjango-drf | 261 | 67–263 | 377 | 526 | 3.8 | 1 | 21 |
| write | 100 | adrf | 128 | 101–129 | 761 | 998 | 8.8 | 107 | 20 |
| write | 100 | drf | 291 | 250–315 | 323 | 556 | 3.6 | 105 | 20 |
| write | 100 | asyncdjango | 379 | 326–390 | 258 | 361 | 2.6 | 1 | 20 |
| write | 100 | django | 258 | 238–267 | 386 | 445 | 4.4 | 106 | 20 |
| slow | 10 | asyncdjango-drf | 143 | 141–149 | 69 | 88 | 1.1 | 1 | 10 |
| slow | 10 | adrf | 124 | 117–140 | 75 | 166 | 3.4 | 16 | 10 |
| slow | 10 | drf | 152 | 146–157 | 66 | 87 | 1.3 | 15 | 10 |
| slow | 10 | asyncdjango | 149 | 146–150 | 66 | 88 | 0.5 | 1 | 10 |
| slow | 10 | django | 127 | 126–138 | 76 | 121 | 2.5 | 16 | 10 |
| slow | 25 | asyncdjango-drf | 302 | 301–304 | 79 | 127 | 1.0 | 1 | 20 |
| slow | 25 | adrf | 253 | 252–268 | 97 | 141 | 2.7 | 32 | 20 |
| slow | 25 | drf | 266 | 265–270 | 91 | 151 | 1.4 | 30 | 20 |
| slow | 25 | asyncdjango | 300 | 299–304 | 80 | 120 | 1.1 | 1 | 20 |
| slow | 25 | django | 270 | 263–272 | 89 | 157 | 2.3 | 31 | 20 |
| slow | 50 | asyncdjango-drf | 300 | 296–304 | 158 | 220 | 0.8 | 1 | 20 |
| slow | 50 | adrf | 271 | 265–274 | 181 | 244 | 2.7 | 57 | 20 |
| slow | 50 | drf | 275 | 274–279 | 179 | 224 | 1.4 | 55 | 20 |
| slow | 50 | asyncdjango | 306 | 303–307 | 160 | 217 | 0.7 | 1 | 20 |
| slow | 50 | django | 280 | 268–285 | 176 | 233 | 2.2 | 56 | 20 |
| slow | 100 | asyncdjango-drf | 302 | 301–304 | 328 | 393 | 1.1 | 1 | 20 |
| slow | 100 | adrf | 268 | 264–279 | 362 | 453 | 2.8 | 107 | 20 |
| slow | 100 | drf | 283 | 268–287 | 351 | 411 | 1.5 | 105 | 20 |
| slow | 100 | asyncdjango | 306 | 306–314 | 325 | 404 | 1.0 | 1 | 20 |
| slow | 100 | django | 278 | 277–283 | 352 | 431 | 2.2 | 106 | 20 |
