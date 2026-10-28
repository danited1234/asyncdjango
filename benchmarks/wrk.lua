-- wrk script for benchmarks/server.py: the requests of BENCH_SCENARIO, as in
-- benchmarks/worker.py, and a JSON summary line at the end.
local scenario = os.getenv("BENCH_SCENARIO") or "read"
local run_id = os.getenv("BENCH_RUN") or "0"
local thread_count = 0

function setup(thread)
  thread_count = thread_count + 1
  thread:set("thread_id", thread_count)
end

function init(args)
  sent = 0
end

function request()
  sent = sent + 1
  if scenario == "read" then
    return wrk.format("GET", "/orgs/" .. (sent % 50 + 1) .. "/")
  elseif scenario == "write" then
    local body = string.format('{"email": "w-%s-%d-%d@example.com", "salary": "1234.50"}',
                               run_id, thread_id, sent)
    return wrk.format("POST", "/orgs/" .. (51 + sent % 10) .. "/employees/",
                      {["Content-Type"] = "application/json"}, body)
  end
  return wrk.format("GET", "/slow/")
end

function done(summary, latency, requests)
  local e = summary.errors
  io.write(string.format(
    'BENCH_JSON {"requests": %d, "duration_us": %d, "socket_errors": %d, ' ..
    '"status_errors": %d, "p50_us": %d, "p95_us": %d, "p99_us": %d, "mean_us": %d}\n',
    summary.requests, summary.duration, e.connect + e.read + e.write + e.timeout,
    e.status, latency:percentile(50), latency:percentile(95), latency:percentile(99),
    math.floor(latency.mean)))
end
