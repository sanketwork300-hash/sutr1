#!/bin/bash
# Runs once, on an empty data directory, before the primary accepts traffic.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    -- A dedicated role with REPLICATION and nothing else: the replica needs to
    -- stream WAL, and it has no business reading application tables with the
    -- application's own credentials.
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '${POSTGRES_PASSWORD}';

    -- A ceiling on any single statement, set on the role rather than sent on
    -- the connection. A client-side `options=-c statement_timeout=...` is a
    -- startup parameter, and PgBouncer refuses startup parameters it does not
    -- know — the application would not start at all. On the role it is applied
    -- by the server to every session, through any pooler, and it cannot be
    -- lost by a connection string someone edits later.
    ALTER ROLE ${POSTGRES_USER} SET statement_timeout = '30s';
    -- Migrations legitimately take longer than a request should, and they run
    -- as the same role.
    ALTER ROLE ${POSTGRES_USER} SET lock_timeout = '10s';
SQL
