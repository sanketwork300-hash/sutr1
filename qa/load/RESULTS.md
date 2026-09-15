# Recorded load-test results

One entry per run. A number without the machine it was taken on is not a
result, so each entry says what it ran against.

---

## 2026-09-01 — first recorded baseline

**What ran:** `qa/load/smoke.js`, 20 VUs, 30 seconds, via
`grafana/k6:latest` over the host network.

**Against:** a single `uvicorn` worker (no `--workers`), SQLite on a local
file, no reverse proxy, tracing off, metrics off. Host: 16 cores, 14 GB RAM,
Linux 7.0.0. The application and the load generator shared that host, so the
generator's own CPU is included in the cost.

**Result:**

| | |
|---|---|
| Requests | 14 572 in 30.1 s — **484 req/s** |
| Iterations | 3 643 — 121/s (each iteration is four requests) |
| Failures | **0** (`http_req_failed` 0.00%) |
| Checks | 14 572 / 14 572 passed |

Per endpoint:

| Path | median | p90 | p95 | max |
|---|---|---|---|---|
| `GET /health/live` | 6.1 ms | 12.1 ms | 14.3 ms | 129 ms |
| `GET /v1/platform/capabilities` | 28.7 ms | 90.4 ms | 143 ms | 1.59 s |
| `GET /v1/observability/summary` | 35.9 ms | 98.9 ms | 148 ms | 1.97 s |
| `POST /v1/discovery/search` | 26.0 ms | 92.4 ms | 142 ms | 1.65 s |

**Reading it.** Liveness at 6 ms median is the floor — the process itself is
not the cost. The other three sit within a few milliseconds of each other,
which says the cost at this concurrency is the shared one (the single worker
and the SQLite writer lock), not anything specific to ranking or aggregation.
The maxima near two seconds are the tail of that same contention: with one
worker, twenty concurrent requests queue.

**What this does NOT establish.** The LLD's targets — 100 000
invocations/minute (1 667 req/s), 10 000 concurrent agents, 1 000 000
searches/day, 99.95% availability — remain **NOT TESTED**. This run is 484
req/s of *read* traffic on one worker against SQLite, with no tool invocation
in it at all: an invocation calls a provider, and the number that would come
back would mostly describe the provider. Nothing here should be quoted as a
capacity figure.

**Worth doing next, in this order:** more than one worker (this ran with one),
PostgreSQL instead of SQLite, a separate load-generator host, and a stubbed
provider so tool invocation can be measured at all.
