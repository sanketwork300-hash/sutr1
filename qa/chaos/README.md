# Chaos scenarios

LLD §5.8 names seven: pod kill, node failure, broker outage, database
failover, latency injection, provider timeout, region isolation.

A chaos test is only worth running against a system that claims to survive the
thing being done to it. So each scenario below names the claim it attacks and
where that claim lives — and the ones with no claim behind them say so instead
of pretending.

| # | Scenario | The claim it attacks | Status |
|---|---|---|---|
| 1 | **Provider timeout / failure** | A failing provider stops being called; other providers are unaffected (ADR-078) | **RUN** — `provider_failure.sh`, and in the suite (`tests/test_resilience/`) |
| 2 | **Pod kill (the leader)** | Singleton work moves to another replica; nothing runs twice (ADR-074) | **RUN** — in Phase 12, by SIGKILLing the lease holder: takeover in 61 s |
| 3 | **Database failover** | Replicas report not-ready rather than serving errors; a standby refuses writes with a reason | **PARTIAL** — the read-only path was exercised in Phase 12; an actual primary→replica promotion was not |
| 4 | **Broker outage** | Facts are not lost: events wait in the outbox | **RUN** — `bus_outage.py`, against the in-process bus |
| 5 | **Latency injection** | Slow providers do not hold workers past the timeout policy | **NOT RUN** — no fault-injection proxy is wired |
| 6 | **Node failure** | Kubernetes reschedules; the PDB keeps a replica serving | **NOT RUN** — needs a cluster; the chart has never been applied to one |
| 7 | **Region isolation** | The surviving region serves reads and refuses writes | **NOT RUN** — needs two regions |

Three of the seven have been run, one partially. The other three need
infrastructure this repository does not have, and saying "not run" is the
honest status for them — a scenario that has never been executed proves
nothing about the system, however carefully its script is written.

## Running them

```bash
# 1. A provider that stops answering, against a running instance.
BASE_URL=http://localhost:8099 API_KEY=... ./provider_failure.sh

# 4. The event bus refusing to publish.
cd server && uv run python ../qa/chaos/bus_outage.py
```
