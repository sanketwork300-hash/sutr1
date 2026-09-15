# Load tests

`smoke.js` measures four paths under a constant load: liveness, an
authenticated capability read, an org-scoped database read, and discovery
search — the one the LLD calls latency-critical.

```bash
k6 run -e BASE_URL=http://localhost:8099 -e API_KEY=... -e VUS=20 -e DURATION=30s qa/load/smoke.js
```

Or without installing k6:

```bash
docker run --rm -i --network host -e BASE_URL=... -e API_KEY=... grafana/k6:latest run - < qa/load/smoke.js
```

## What a result here is, and is not

The LLD's cover targets — 100 000 invocations/minute, 10 000 concurrent
agents, 1 000 000 searches/day, 99.95% availability — are **design targets and
remain NOT TESTED**. This script cannot establish them and a number from it
must never be quoted as though it had: one container, one machine, one SQLite
file is not the deployment those numbers describe.

What it is for: a repeatable number per endpoint, so a change that makes the
gateway several times slower is noticed by somebody other than a customer. The
thresholds are loose regression tripwires for exactly that reason.

`RESULTS.md` records what was measured, on what, and when.
