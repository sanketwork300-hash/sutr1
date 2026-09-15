# Running more than one (ESDS LLD §5.4, §5.7, §5.8)

The LLD asks for HA PostgreSQL, Kafka, Redis, OpenSearch, Qdrant, Neo4j, MinIO
and Vault, and for a multi-region deployment where regions upgrade
independently.

This directory ships what that means *for this build*, which is less, and says
where the difference is.

## What is here

| | |
|---|---|
| `docker-compose.ha.yml` | Two API replicas behind a load balancer, PgBouncer, a PostgreSQL primary with a streaming replica, and the event relay as its own process. **Started and exercised**; see "What was verified". |
| `Caddyfile.ha` | The load balancer, health-checking replicas on `/health/ready`. |
| `postgres/` | `pg_hba.conf` with a replication rule, and the init script that creates the replication role and sets the statement timeout on the application role. |
| `helm/sutr/` | The chart: API Deployment with real probes, a relay Deployment, migrations as a pre-upgrade hook, PDB, optional HPA and Ingress. **Rendered and schema-validated, never run on a live cluster.** |
| `backup/` | `backup.sh` and `restore.sh`, with a manifest, a checksum and a restore that verifies rather than merely finishing. **Round-tripped for real**; see below. |

## The stores the LLD names, and whether this build uses them

An HA chart for a database the application never opens a connection to would
be infrastructure theatre. So:

| Store | Used by this build? | Status |
|---|---|---|
| PostgreSQL | **Yes** — the one authoritative store | Primary + streaming replica in the compose stack; single primary, **no automatic failover** |
| Kafka | Optional — the event bus has a Kafka backend (`EVENT_BUS=kafka`) | **NOT IMPLEMENTED here.** No broker is shipped; the default in-process bus needs none, and the outbox makes the bus a delivery mechanism rather than a system of record |
| Redis | **No.** Caches are per process and declared at `/v1/platform/scaling` | **NOT IMPLEMENTED** |
| OpenSearch / Elasticsearch | **No.** Discovery's lexical retrieval is SQL | **NOT IMPLEMENTED** |
| Qdrant / Milvus / pgvector | **No.** Embeddings are stored in `chunk_embedding` | **NOT IMPLEMENTED** |
| Neo4j | **No.** The knowledge graph is `knowledge_node` / `knowledge_edge` | **NOT IMPLEMENTED** |
| MinIO / S3 | **No.** Documents are in the database or on a filesystem | **NOT IMPLEMENTED** |
| Vault | Optional secrets backend, **NOT TESTED against a live Vault** | No Vault is shipped here |

Six of those are "not implemented" because the application has no client for
them, not because the deployment is incomplete. When one of them arrives, its
HA configuration arrives with it.

## What the application itself does about running twice

This is the part that is this repository's to get right, and it is code rather
than YAML:

- **Work that must run once runs under a lease.** The maintenance sweep, the
  deployment sweep, source sync and the event relay each take a row in
  `leader_lease`; one replica holds it, renews it while it works, and another
  takes over when it stops. The deployment sweep meters runtime minutes, so two
  replicas running it would bill a tenant twice for the same five minutes
  (ADR-074). `GET /v1/platform/leadership` says who holds what.
- **Liveness and readiness are separate.** `/health/live` checks the process
  and nothing else; `/health/ready` checks the database. A liveness probe that
  failed during a database outage would restart every replica at once.
- **A standby refuses writes with a reason.** `PLATFORM_MODE=standby` serves
  reads and answers writes with a 503 naming the region that owns them, instead
  of letting a read-only replica produce *"cannot execute INSERT in a read-only
  transaction"* as a 500 (ADR-075).
- **What is still per-process is written down.** Rate-limit windows, the
  discovery cache, approval long-poll waiters and the metrics registry are
  per replica by design; `GET /v1/platform/scaling` lists each one with what
  changes when there is more than one.

## What was verified, and how

Run against the stack in this directory, on one host:

- **Replication.** `pg_stat_replication` showed a streaming async replica with
  replay lag under 2 ms. A row written on the primary was readable on the
  replica; a write attempted *against* the replica was refused with `cannot
  execute INSERT in a read-only transaction`.
- **Migrations through the pooler.** `alembic upgrade head` ran to 0043 through
  PgBouncer in transaction mode, before any replica served.
- **Load balancing.** Alternating requests through Caddy were answered by two
  different `instance_id`s.
- **Leadership.** Exactly one replica held all four leases; the other held
  none. Stopping the leader (SIGTERM) handed the leases over; **killing** it
  (SIGKILL, no chance to release) was followed by a takeover 61 seconds later —
  a 45-second lease plus a 15-second poll — with the lease generation
  incrementing from 2 to 3.
- **Standby.** A third instance pointed at the read-only replica with
  `PLATFORM_MODE=standby` reported ready, served `GET`s, answered
  `POST /v1/discovery/search` (allow-listed, writes nothing) and refused
  `POST /v1/registry/tools` with a 503 naming the primary region.
- **Backup and restore.** A 260 KB custom-format dump was taken from the live
  database and restored into a fresh one: 73 tables, schema 0043, checksum
  verified. Restoring over a non-empty database was refused without `FORCE=1`,
  and a dump edited after its manifest was written was refused outright.
- **The chart.** `helm lint` and `helm template` pass; the nine rendered
  manifests validate strictly against Kubernetes 1.31 schemas with
  `kubeconform`. It refuses to render without an image, with a `latest` tag, or
  without a Secret name.

## What was NOT verified, and must not be claimed

- **Availability numbers.** §5.4 asks for 99.99% on PostgreSQL and §5.8 for
  99.95% platform availability. Nothing here has been measured over any period.
  **NOT TESTED.**
- **Automatic failover.** There is **no automatic failover**. The replica is a
  warm copy; promotion is `pg_ctl promote` plus repointing PgBouncer, both
  deliberate acts. The LLD's connection path names a proxy between the pooler
  and the database for exactly this; there is none here, and an unconfigured
  HAProxy would only look like one.
- **The Helm chart on a cluster.** Rendered and schema-checked, never applied.
  **NOT TESTED.**
- **One host.** Every "replica" above shares a kernel, a disk and a power
  supply. What was demonstrated is that the *software* is correct with more
  than one of it — not that the deployment is redundant.
- **Cross-region anything.** Standby mode is a foundation, not a failover
  system: nothing here promotes a standby, routes traffic between regions, or
  measures replication lag between them.

## Running it

```bash
export JWT_SECRET_KEY=$(openssl rand -hex 32)   # the same one for every replica
export POSTGRES_PASSWORD=...
docker compose -f deploy/ha/docker-compose.ha.yml up -d --build
curl -s localhost:8080/health/ready | jq '{instance_id, leadership}'
```

Two things bite, both learned by running it:

1. **`DB_STATEMENT_TIMEOUT_MS` must not be set behind PgBouncer.** It travels as
   a libpq startup parameter, and PgBouncer rejects startup parameters it does
   not know — every replica dies at boot with *"unsupported startup parameter
   in options: statement_timeout"*. Set it on the role instead, which
   `postgres/init-replication.sh` does.
2. **Every replica needs the same `JWT_SECRET_KEY`.** Otherwise a session
   issued by one replica is rejected by the next, and the symptom is an
   intermittent logout rather than an error.
