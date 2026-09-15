# Runbooks

One section per alert in `alerts.yml`. Every alert carries a `runbook`
annotation naming the section it belongs to, and
`server/tests/test_observability/test_deployment_assets.py` fails the build if
an alert names a section that is not here — a runbook link that goes nowhere is
worse than none, because it is read at three in the morning.

Each section says what the alert means, what is *not* broken (which is usually
the more useful half), and what to look at first.

---

## sutr-instance-down — the scrape target is not answering

**Means:** Prometheus could not scrape `/metrics` for two minutes.

**Check first:** whether the process is down or only the endpoint is. `/metrics`
is disabled unless `METRICS_ENABLED=true`, and returns **404** when it is off —
so a freshly deployed instance with the flag unset produces this alert while
serving traffic perfectly. `curl -sf http://<host>/health` distinguishes the two
in one command: healthy plus this alert means the flag, not the process.

**If the process is genuinely down:** tool calls are failing, and nothing is
being metered while it is down. Usage already recorded is durable; usage that
would have happened is simply absent.

---

## sutr-high-error-rate — more than one request in twenty is failing

**Means:** 5xx responses are over 5% of all requests, averaged over five
minutes.

**Check first:** whether the errors are concentrated on one route. The
`sutr_http_requests_total` series is labelled by route template, so
`topk(5, sum by (route) (rate(sutr_http_requests_total{status=~"5.."}[5m])))`
names the culprit immediately.

**Note:** a provider returning 500 does *not* raise this. A failed tool call is
a successful HTTP response carrying an error result — see
`sutr-provider-errors` for that.

---

## sutr-high-request-latency — p95 is above two seconds

**Means:** the slowest one request in twenty took over two seconds, handler-side.

**Check first:** the provider latency panel. This platform's own work is
milliseconds; almost all of a slow request is time spent waiting for a provider,
and the two alerts firing together mean the upstream is slow rather than this
platform.

**If provider latency is normal:** look at the database. Every tool call takes
several short-lived sessions, and a saturated connection pool shows up here
first.

---

## sutr-provider-errors — one provider call in five is failing

**Means:** provider requests are erroring across the platform, aggregated.
`/metrics` carries no provider label deliberately (cardinality, and disclosure
between tenants), so this alert cannot tell you *which* provider.

**Check first:** the affected tenant's own telemetry, which is per-provider:
`GET /v1/observability/summary` returns `by_provider` for the calling
organisation. Failing that, the call log — `/api/logs` — is filterable by
integration and outcome.

---

## sutr-provider-latency — p95 provider latency is above five seconds

**Means:** upstream systems are slow. Agents inherit this directly: their call
is waiting on the same socket.

**Check first:** whether it is one provider or all of them. All of them usually
means egress — DNS, a proxy, or the network — rather than a coincidence of
provider incidents.

---

## sutr-tool-calls-gated — calls are being refused for quota

**Means:** at least one tenant is hitting a quota and its calls are being
refused before execution.

**This is not necessarily a fault.** A quota that is being enforced is a quota
doing its job. It is informational and rate-limited to a daily repeat for that
reason.

**Check:** `GET /api/quotas` for the affected organisation. If the quota is
wrong, raise it; if the caller is wrong, the refusals are the correct outcome.

---

## sutr-event-relay-lagging — the oldest unpublished event is over five minutes old

**Means:** the transactional outbox has rows the relay has not published.

**What is not broken:** nothing has been lost. Every event is written in the
same transaction as the state change it describes, so the facts are durable;
what is late is everything downstream — consumers, and any metering that rides
the bus.

**Check first:** whether the relay is running at all. In a single-container
install it is a background loop inside the API process; with more than one
replica it is the separate `sutr-event-relay` process. A relay that is not
running produces exactly this alert alongside a growing `outbox_pending`.

---

## sutr-dead-lettered-events — events exhausted their retries

**Means:** one or more events failed to publish five times and were moved to
`dead_lettered`. Nothing will retry them automatically; that is what the state
means.

**Check first:** `last_error` on the affected rows, through
`GET /v1/events/dead-letter`. The usual causes are a bus that was down long
enough to exhaust attempts, and a consumer rejecting a payload it should have
accepted.

---

## sutr-outbox-backlog — the outbox is over a thousand rows deep

**Means:** events are being produced faster than they are published, or the
relay stopped.

**Check first:** `sutr_event_relay_lag_seconds`. Depth with low lag is a burst
being worked through; depth with rising lag is a relay that is not keeping up.

---

## sutr-telemetry-sampling-failing — the queue gauges could not be sampled

**Means:** the scrape endpoint could not read the database. Queue depth and
relay lag are therefore **stale, not zero** — the previous values are still
being served.

**Consequence:** treat `sutr-event-relay-lagging`, `sutr-outbox-backlog` and
`sutr-dead-lettered-events` as unreliable until this clears. They are computed
from those gauges.

**Check first:** the database. This counter increments only when a `SELECT
count(*)` against `outbox_event` raised.

---

## sutr-deployments-failed — a generated runtime is in the failed state

**Means:** at least one deployment was observed in `failed` by the monitoring
sweep, which runs every five minutes — so this number is up to five minutes old
by construction.

**What is not broken:** every other tool. A failed deployment affects the tools
that runtime serves and nothing else.

**Check first:** `GET /api/deployments` for the failing one, then its logs
through `GET /api/deployments/{id}/logs`.
