# Observability stack (ESDS LLD §5.3)

The LLD names eight components in a dedicated namespace — otel-collector,
prometheus, grafana, loki, tempo, alertmanager, node-exporter,
kube-state-metrics — and requires that they all run HA.

This directory ships **seven of them as a single-replica local stack**, pinned
to exact versions, with configuration that is validated rather than asserted.

| Component | Here | Status |
|---|---|---|
| otel-collector | `otel-collector.yaml` | **IMPLEMENTED**, single replica |
| prometheus | `prometheus.yml` + `alerts.yml` | **IMPLEMENTED**, single replica, local TSDB |
| alertmanager | `alertmanager.yml` | **PARTIAL** — routing and inhibition are real; the receivers deliberately go nowhere |
| loki | `loki.yaml` | **IMPLEMENTED**, single binary, filesystem storage |
| tempo | `tempo.yaml` | **IMPLEMENTED**, single binary, local storage |
| grafana | `grafana/` | **IMPLEMENTED** — datasources and one dashboard, provisioned from files |
| node-exporter | in the compose file | **IMPLEMENTED** |
| Fluent Bit | `fluent-bit.conf` | **NOT TESTED** — the config is written and commented out of the compose file; it needs a privileged host mount |
| kube-state-metrics | — | **NOT IMPLEMENTED** — it reports on a Kubernetes cluster, and this is a compose file |
| HA (replicas / federation / clustering) | — | **NOT IMPLEMENTED** — Phase 12 |
| mTLS collector ↔ service | commented block in `otel-collector.yaml`, wired on the application side | **PARTIAL** — the application settings reach the exporter and are tested; no certificates are shipped, and the receiver's TLS block is commented out |

## Running it

```bash
docker compose -f deploy/observability/docker-compose.observability.yml up -d
```

Then point the application at it:

```bash
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces
METRICS_ENABLED=true
LOG_FORMAT=json
```

Grafana is on `:3000` (admin / `GRAFANA_ADMIN_PASSWORD`, default `admin` —
change it before anyone else can reach it), Prometheus on `:9090`, Alertmanager
on `:9093`, Tempo on `:3200`, Loki on `:3100`.

## What is checked, and by what

`server/tests/test_observability/test_deployment_assets.py` asserts that:

- every metric named in `alerts.yml` and in the dashboard is a metric this build
  actually exports — an alert on a series nobody emits never fires, and nobody
  ever notices;
- every alert names a runbook section that exists in `RUNBOOKS.md`;
- every alert carries a severity and a summary;
- the compose file, the Grafana provisioning and the dashboard are parseable,
  and the images are pinned to exact versions rather than `latest`.

The configuration files themselves were validated against the real binaries at
the pinned versions — `promtool check config`, `amtool check-config`,
`otelcol validate`, `loki -verify-config`, `tempo -config.verify` — and the
whole stack was started and queried end to end. That validation is **not** part
of the test suite: it needs Docker and network access, which CI for this
repository does not have. It is a thing that was done, not a thing that is
guaranteed on every commit.

## Tenancy

Loki and Tempo run single-tenant here, and that is deliberate. What ships to
them is the platform's own telemetry, which spans every organisation; a
per-tenant view built on `X-Scope-OrgID` would be enforced by whoever sets the
header, which is not isolation.

Tenant-isolated telemetry is served instead by `/v1/observability`, from this
platform's own tables, scoped to the calling organisation by the query itself.
A tenant never reaches Prometheus, Loki or Tempo.
