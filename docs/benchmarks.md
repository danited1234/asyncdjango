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

### Behind a real server (Linux)

`benchmarks/server.py` runs the same scenarios against each stack in its
production shape: one uvicorn worker (uvloop, httptools) for the ASGI stacks
and one gunicorn `gthread` worker with `concurrency` threads for the WSGI
stack, driven over loopback HTTP by [wrk](https://github.com/wg/wrk)
(`benchmarks/wrk.lua` sends each scenario's requests). The server and wrk are
pinned to separate CPUs with `taskset`; keep PostgreSQL on others. It needs
Linux with `wrk` installed:

```console
pip install -e ".[drf,bench-server]" "psycopg[binary]"
python -m benchmarks.server --repeat 3 --server-cpus 0,1 --client-cpus 2,3
docker compose -f benchmarks/docker/compose.yaml run --rm bench   # or in Docker
```

The `server benchmark` workflow runs it on GitHub's 4-vCPU Linux runners (one
job per scenario; PostgreSQL on vCPU 3, the server on 0-1, wrk on 2) and
publishes every round as an annotation on the job. Results use the same file
format as `run.py`, so `benchmarks/plot.py` charts them.

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
  clients stayed between 270 and 390. Behind a real server on Linux (below)
  the same case stays within 9% across rounds (202–220 req/s for read,
  245–250 for write), so the spread comes from this in-process Windows setup,
  where the load generator shares the event loop with the stack.

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

## Results behind a real server (Linux)

Measured on 2026-10-28 by the `server benchmark` workflow on a GitHub-hosted
`ubuntu-latest` runner (x86_64, 4 vCPUs): Python 3.13, Django 6.0.3, DRF
3.16.1, adrf 0.1.12, psycopg 3.3.6, PostgreSQL 18 in a container on vCPU 3,
the server on vCPUs 0–1, wrk on vCPU 2. `python -m benchmarks.server
--repeat 3 --duration 8 --warmup 2`, a 20-connection pool, one job per
scenario. Every round is in
[`benchmarks/results/server-github-2026-10-28.csv`](../benchmarks/results/server-github-2026-10-28.csv).
No request failed in any round.

- **asyncdjango.rest_framework versus ADRF.** 8.6 times the read throughput
  (214 against 25 req/s at 50 clients, at a ninth of the CPU per request) and
  1.8 times the write throughput, as in-process.
- **asyncdjango versus Django's async views.** 28% more reads and 35% more
  writes at 50 clients (313 against 245, 389 against 289 req/s), with 31–36%
  less CPU per request.
- **asyncdjango.rest_framework versus synchronous DRF under gunicorn.** DRF
  serves 10–22% more reads and writes when the server is CPU-bound, although
  their CPU per request is about the same (3.5–4.9 ms). The event loop is one
  thread and so is capped at one core (214 req/s × 4.7 ms ≈ 1.0), while DRF's
  worker threads overlap a little inside psycopg and the socket layer (about
  1.15 cores). In exchange asyncdjango runs on 5 threads instead of 107 at 100
  clients, and its 99th-percentile latency stays lower: 979 against 5,196 ms
  for read and 878 against 3,715 ms for write at 100 clients. Run one worker
  per core, as in production, to use the other cores.
- **Database-bound requests.** The `slow` scenario is capped by the pool (20
  connections × 50 ms = 400 req/s). The asyncdjango stacks reach 380–389
  req/s, the others 350–388. Plain asyncdjango uses the least CPU per request
  (1.3–1.4 ms), asyncdjango.rest_framework and synchronous DRF 1.4–1.7, ADRF
  and Django's async views 2.4–3.1.
- **Stability.** In this run every case's rounds stay within 10% of each
  other, including asyncdjango.rest_framework at 100 clients, which varied by
  a factor of up to four in the in-process Windows runs. Later runs were less
  even: plain asyncdjango's reads at 100 clients ranged 470–585 req/s in one,
  and asyncdjango.rest_framework's 352–430 with the server on one core (see
  below). Treat differences under about 15% between single rounds as noise.

| scenario | clients | stack | req/s (median) | req/s range | p50 ms | p99 ms | CPU ms/request | threads | DB connections |
|---|---|---|---|---|---|---|---|---|---|
| read | 25 | asyncdjango-drf | 228 | 219–234 | 105 | 197 | 4.4 | 5 | 20 |
| read | 25 | adrf | 25 | 25–25 | 800 | 1451 | 43.9 | 38 | 20 |
| read | 25 | drf | 251 | 246–256 | 96 | 171 | 4.6 | 32 | 20 |
| read | 25 | asyncdjango | 328 | 325–331 | 75 | 98 | 3.1 | 5 | 20 |
| read | 25 | django | 255 | 253–262 | 96 | 120 | 4.5 | 45 | 20 |
| read | 50 | asyncdjango-drf | 214 | 210–228 | 215 | 401 | 4.7 | 5 | 20 |
| read | 50 | adrf | 25 | 25–25 | 1618 | 2316 | 43.7 | 63 | 20 |
| read | 50 | drf | 246 | 246–251 | 194 | 873 | 4.7 | 57 | 20 |
| read | 50 | asyncdjango | 313 | 310–326 | 158 | 198 | 3.2 | 5 | 20 |
| read | 50 | django | 245 | 244–255 | 201 | 283 | 4.6 | 70 | 20 |
| read | 100 | asyncdjango-drf | 205 | 202–220 | 441 | 979 | 4.9 | 5 | 20 |
| read | 100 | adrf | 25 | 25–25 | 3751 | 4021 | 44.0 | 116 | 20 |
| read | 100 | drf | 240 | 238–246 | 352 | 5196 | 4.8 | 107 | 20 |
| read | 100 | asyncdjango | 298 | 298–319 | 324 | 743 | 3.4 | 5 | 20 |
| read | 100 | django | 241 | 234–250 | 400 | 944 | 4.7 | 120 | 20 |
| write | 25 | asyncdjango-drf | 286 | 282–290 | 85 | 163 | 3.5 | 5 | 20 |
| write | 25 | adrf | 150 | 149–153 | 163 | 219 | 7.4 | 39 | 21 |
| write | 25 | drf | 318 | 313–327 | 76 | 134 | 3.7 | 32 | 20 |
| write | 25 | asyncdjango | 424 | 424–427 | 58 | 85 | 2.4 | 5 | 20 |
| write | 25 | django | 309 | 299–310 | 79 | 103 | 3.8 | 45 | 20 |
| write | 50 | asyncdjango-drf | 265 | 265–271 | 182 | 282 | 3.8 | 5 | 21 |
| write | 50 | adrf | 147 | 147–150 | 331 | 413 | 7.6 | 64 | 21 |
| write | 50 | drf | 310 | 300–315 | 155 | 537 | 3.8 | 57 | 20 |
| write | 50 | asyncdjango | 389 | 385–400 | 126 | 191 | 2.6 | 5 | 21 |
| write | 50 | django | 289 | 286–296 | 170 | 233 | 4.0 | 72 | 20 |
| write | 100 | asyncdjango-drf | 246 | 245–250 | 375 | 878 | 4.1 | 5 | 21 |
| write | 100 | adrf | 142 | 139–146 | 672 | 980 | 7.8 | 115 | 20 |
| write | 100 | drf | 299 | 297–303 | 304 | 3715 | 4.0 | 107 | 21 |
| write | 100 | asyncdjango | 371 | 369–374 | 260 | 529 | 2.7 | 5 | 20 |
| write | 100 | django | 286 | 285–287 | 340 | 735 | 4.1 | 120 | 20 |
| slow | 25 | asyncdjango-drf | 388 | 387–388 | 63 | 80 | 1.6 | 5 | 20 |
| slow | 25 | adrf | 369 | 366–369 | 66 | 86 | 2.9 | 37 | 20 |
| slow | 25 | drf | 388 | 388–388 | 62 | 83 | 1.4 | 32 | 20 |
| slow | 25 | asyncdjango | 389 | 389–390 | 61 | 83 | 1.3 | 5 | 20 |
| slow | 25 | django | 377 | 377–378 | 65 | 82 | 2.4 | 40 | 20 |
| slow | 50 | asyncdjango-drf | 385 | 383–386 | 128 | 167 | 1.6 | 5 | 20 |
| slow | 50 | adrf | 365 | 362–367 | 134 | 182 | 3.0 | 93 | 20 |
| slow | 50 | drf | 384 | 383–385 | 128 | 161 | 1.4 | 57 | 20 |
| slow | 50 | asyncdjango | 388 | 387–390 | 127 | 149 | 1.4 | 5 | 20 |
| slow | 50 | django | 373 | 373–374 | 131 | 172 | 2.4 | 93 | 20 |
| slow | 100 | asyncdjango-drf | 380 | 380–381 | 256 | 337 | 1.6 | 5 | 20 |
| slow | 100 | adrf | 350 | 348–351 | 270 | 432 | 3.1 | 173 | 20 |
| slow | 100 | drf | 376 | 376–377 | 256 | 399 | 1.5 | 107 | 20 |
| slow | 100 | asyncdjango | 386 | 385–386 | 255 | 282 | 1.4 | 5 | 20 |
| slow | 100 | django | 364 | 363–364 | 265 | 438 | 2.5 | 175 | 20 |

## Results with the server on one CPU (Linux)

Measured on 2026-10-29 by the same workflow with the server pinned to one
core (`--server-cpus 0`), the shape of a small VM, next to the same jobs on
two cores. Everything else as above. Each job ran on its own runner, and the
one-core jobs happened to get faster processors (about half the CPU time per
request), so compare stacks within one table, never the one-core numbers with
the two-core ones. Every round, with peak memory, is in
[`benchmarks/results/server-github-2026-10-29.csv`](../benchmarks/results/server-github-2026-10-29.csv).
No request failed.

- **Plain asyncdjango is fastest on one core.** 23% more reads and 14% more
  writes than Django's async views at 50 clients (658 against 535, 423 against
  372 req/s), and 35% and 12% more than synchronous DRF, using 18–26% less
  CPU per read and 11–12% less per write than either.
- **asyncdjango.rest_framework still trails synchronous DRF** on these
  CPU-bound endpoints: 15% fewer reads and 22% fewer writes at 50 clients
  (414 against 486, 296 against 379 req/s). With one core DRF's threads
  cannot overlap any more, and the gap stays because asyncdjango's DRF layer
  costs more CPU per request (2.4 against 2.1 ms read, 3.4 against 2.6 ms
  write). That overhead, not the event loop, is what to optimize.
- **Against ADRF:** 5.5 times the reads and 1.6 times the writes.
- **Memory.** The event-loop stacks use less: at 100 clients plain asyncdjango
  peaks at 91 MB and asyncdjango.rest_framework at 114 MB, against 139 MB for
  synchronous DRF's gunicorn worker with 107 threads (memory also grows with
  the number of open connections and requests in flight).
- **Tail latency** depends on the core count. On two cores synchronous DRF's
  slowest requests waited up to 5.2 s (read) and 4.6 s (write) at 100
  clients, against 0.9 and 0.8 s for asyncdjango.rest_framework, as in the
  first run. On one core DRF's tail is normal (503 and 469 ms against 466 and
  691 ms), so the long waits most likely come from its threads competing for
  the GIL across cores.
- **Database-bound requests** stay capped by the pool at 357–391 req/s on
  either core count; asyncdjango is highest at every client count.

Server on one core, median of three rounds:

| scenario | clients | stack | req/s (median) | req/s range | p50 ms | p99 ms | CPU ms/request | threads | peak MB |
|---|---|---|---|---|---|---|---|---|---|
| read | 25 | asyncdjango | 667 | 662–669 | 36 | 69 | 1.5 | 5 | 75 |
| read | 25 | django | 579 | 572–585 | 42 | 70 | 1.7 | 45 | 74 |
| read | 25 | drf | 510 | 489–521 | 47 | 111 | 2.0 | 32 | 106 |
| read | 25 | asyncdjango-drf | 458 | 453–469 | 51 | 121 | 2.2 | 5 | 93 |
| read | 25 | adrf | 77 | 75–77 | 281 | 505 | 13.0 | 38 | 92 |
| read | 50 | asyncdjango | 658 | 644–690 | 74 | 115 | 1.5 | 5 | 81 |
| read | 50 | django | 535 | 526–568 | 92 | 152 | 1.9 | 71 | 80 |
| read | 50 | drf | 486 | 460–498 | 94 | 243 | 2.1 | 57 | 120 |
| read | 50 | asyncdjango-drf | 414 | 374–416 | 108 | 290 | 2.4 | 5 | 109 |
| read | 50 | adrf | 75 | 75–75 | 600 | 825 | 13.4 | 65 | 113 |
| read | 100 | asyncdjango | 632 | 618–680 | 152 | 259 | 1.6 | 5 | 91 |
| read | 100 | django | 531 | 506–542 | 180 | 313 | 1.9 | 126 | 90 |
| read | 100 | drf | 432 | 428–449 | 196 | 503 | 2.3 | 107 | 139 |
| read | 100 | asyncdjango-drf | 407 | 352–430 | 215 | 466 | 2.5 | 5 | 114 |
| read | 100 | adrf | 75 | 72–75 | 1261 | 1503 | 13.4 | 114 | 115 |
| write | 25 | asyncdjango | 458 | 452–459 | 54 | 75 | 2.2 | 5 | 74 |
| write | 25 | django | 385 | 379–391 | 64 | 88 | 2.6 | 42 | 74 |
| write | 25 | drf | 388 | 381–390 | 63 | 115 | 2.6 | 32 | 98 |
| write | 25 | asyncdjango-drf | 310 | 309–318 | 79 | 118 | 3.2 | 5 | 83 |
| write | 25 | adrf | 190 | 183–190 | 129 | 178 | 5.3 | 38 | 83 |
| write | 50 | asyncdjango | 423 | 419–432 | 116 | 161 | 2.4 | 5 | 79 |
| write | 50 | django | 372 | 371–379 | 134 | 179 | 2.7 | 70 | 79 |
| write | 50 | drf | 379 | 378–385 | 129 | 198 | 2.6 | 57 | 105 |
| write | 50 | asyncdjango-drf | 296 | 295–299 | 165 | 237 | 3.4 | 5 | 91 |
| write | 50 | adrf | 184 | 183–186 | 265 | 345 | 5.4 | 63 | 93 |
| write | 100 | asyncdjango | 419 | 416–420 | 232 | 450 | 2.4 | 5 | 88 |
| write | 100 | django | 370 | 370–373 | 261 | 582 | 2.7 | 121 | 89 |
| write | 100 | drf | 372 | 370–378 | 256 | 469 | 2.7 | 107 | 119 |
| write | 100 | asyncdjango-drf | 285 | 282–286 | 334 | 691 | 3.5 | 5 | 103 |
| write | 100 | adrf | 180 | 178–182 | 531 | 767 | 5.5 | 113 | 110 |
| slow | 100 | asyncdjango | 384 | 384–385 | 255 | 312 | 1.4 | 5 | 85 |
| slow | 100 | django | 367 | 366–370 | 262 | 437 | 1.9 | 167 | 86 |
| slow | 100 | drf | 372 | 371–372 | 257 | 446 | 1.3 | 107 | 101 |
| slow | 100 | asyncdjango-drf | 379 | 378–380 | 256 | 323 | 1.7 | 5 | 96 |
| slow | 100 | adrf | 359 | 356–359 | 264 | 428 | 2.3 | 165 | 96 |

## Results on a 2-core, 2 GB machine (Linux)

Measured on 2026-10-29 with the shape of a small VPS whose database is a
separate host: the server on two vCPUs with one worker per core
(`--workers 2`: two uvicorn workers, or two gunicorn workers whose threads add
up to the client count), each worker with half of the 20-connection pool, and
the whole job in a 2 GB cgroup without swap. GitHub's 4-vCPU runners are most
likely two physical cores with two hyperthreads each, so the two vCPUs here
are probably one core's hyperthreads, as on many VPS plans. This job ran on
an AMD EPYC 7763 (read, write) or EPYC 9V45 (slow). Every round is in
[`benchmarks/results/server-github-2026-10-29-vm.csv`](../benchmarks/results/server-github-2026-10-29-vm.csv),
next to a one-core job of the same run. No request failed and no stack came
near the memory limit.

- **Plain asyncdjango is fastest.** At 50 clients it serves 15% more reads
  than Django's async views and 11% more than synchronous DRF (420 against
  364 and 378 req/s), and 17% more writes than Django (564 against 482), level
  with DRF's 553.
- **asyncdjango.rest_framework trails synchronous DRF more here than on one
  core**: 22% fewer reads and 31% fewer writes at 50 clients (296 against 378,
  384 against 553 req/s), at 28% and 45% more CPU per request. Its DRF layer
  is the bottleneck; see the profiling item in todo.md.
- **Against ADRF:** 7.9 times the reads and 1.6 times the writes.
- **Memory fits easily.** The busiest stack peaked at 256 MB for both workers
  together (asyncdjango.rest_framework at 100 clients); plain asyncdjango
  197 MB, synchronous DRF 210 MB, Django 191 MB. A 2 GB machine has room for
  more workers, or for the rest of the system.
- **Tail latency** at 100 clients: plain asyncdjango's slowest 1% of reads took
  279 ms against 611 ms for DRF and 388 ms for Django; asyncdjango.rest_framework
  575 ms.
- **Uneven workers.** In the database-bound `slow` scenario at 25 clients some
  rounds of several stacks dropped to about 255 req/s from 390: each worker's
  share of the pool is 10 connections, and a keep-alive connection stays with
  the worker that accepted it, so an uneven split of clients caps one worker
  early. With more clients the split evens out (391–394 req/s for the
  asyncdjango stacks at 50 and 100 clients).

Two cores and 2 GB, two workers, median of three rounds:

| scenario | clients | stack | req/s (median) | req/s range | p50 ms | p99 ms | CPU ms/request | threads | peak MB |
|---|---|---|---|---|---|---|---|---|---|
| read | 50 | asyncdjango | 420 | 415–427 | 104 | 160 | 4.8 | 6 | 187 |
| read | 50 | django | 364 | 360–372 | 137 | 182 | 5.3 | 74 | 184 |
| read | 50 | drf | 378 | 367–383 | 129 | 248 | 5.3 | 62 | 185 |
| read | 50 | asyncdjango-drf | 296 | 293–301 | 163 | 320 | 6.8 | 6 | 223 |
| read | 50 | adrf | 38 | 38–38 | 1094 | 1537 | 53.4 | 69 | 200 |
| read | 100 | asyncdjango | 418 | 415–419 | 236 | 279 | 4.8 | 6 | 197 |
| read | 100 | django | 364 | 360–364 | 268 | 388 | 5.4 | 127 | 191 |
| read | 100 | drf | 351 | 351–361 | 315 | 611 | 5.6 | 112 | 210 |
| read | 100 | asyncdjango-drf | 276 | 273–277 | 342 | 575 | 7.2 | 6 | 256 |
| read | 100 | adrf | 37 | 36–37 | 2575 | 2689 | 53.4 | 119 | 209 |
| write | 50 | asyncdjango | 564 | 541–569 | 91 | 138 | 3.5 | 6 | 188 |
| write | 50 | django | 482 | 480–488 | 103 | 134 | 4.1 | 76 | 186 |
| write | 50 | drf | 553 | 552–562 | 89 | 150 | 3.6 | 62 | 171 |
| write | 50 | asyncdjango-drf | 384 | 378–395 | 129 | 222 | 5.2 | 6 | 206 |
| write | 50 | adrf | 233 | 231–235 | 150 | 378 | 8.5 | 72 | 198 |
| write | 100 | asyncdjango | 556 | 528–572 | 64 | 425 | 3.6 | 6 | 200 |
| write | 100 | django | 471 | 459–473 | 207 | 293 | 4.1 | 128 | 192 |
| write | 100 | drf | 545 | 538–549 | 179 | 349 | 3.6 | 99 | 182 |
| write | 100 | asyncdjango-drf | 352 | 344–376 | 295 | 536 | 5.7 | 6 | 224 |
| write | 100 | adrf | 223 | 221–234 | 437 | 524 | 8.8 | 120 | 207 |
| slow | 100 | asyncdjango | 394 | 392–394 | 198 | 355 | 0.8 | 6 | 192 |
| slow | 100 | django | 379 | 379–380 | 256 | 403 | 1.5 | 185 | 190 |
| slow | 100 | drf | 384 | 382–384 | 274 | 541 | 0.8 | 112 | 163 |
| slow | 100 | asyncdjango-drf | 392 | 389–393 | 202 | 347 | 1.1 | 6 | 201 |
| slow | 100 | adrf | 377 | 372–378 | 305 | 462 | 1.8 | 186 | 201 |
