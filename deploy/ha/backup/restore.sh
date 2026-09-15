#!/usr/bin/env bash
# Restore a backup, and then check that what came back is usable.
#
# A restore that finishes is not a restore that worked. This one ends by
# comparing the schema version in the restored database against the manifest,
# and by counting the tables that came back — the two things that distinguish
# "restored" from "restored empty".
#
# Refuses to write into a database that already has data unless FORCE=1: the
# realistic way to lose a production database is to restore last week's backup
# over it while looking for a staging URL.
#
# Usage:
#   TARGET_URL=postgresql://user:pass@host:5432/sutr_restore ./restore.sh sutr-2026...dump
set -euo pipefail

: "${TARGET_URL:?set TARGET_URL to the database to restore INTO}"
DUMP="${1:?pass the .dump file to restore}"
[ -f "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }

LIBPQ_URL="${TARGET_URL/+psycopg2/}"
LIBPQ_URL="${LIBPQ_URL/+psycopg/}"

MANIFEST="${DUMP%.dump}.manifest.json"
if [ -f "$MANIFEST" ]; then
    EXPECTED_SHA="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['sha256'])" "$MANIFEST")"
    ACTUAL_SHA="$(sha256sum "$DUMP" | cut -d' ' -f1)"
    if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
        echo "checksum mismatch: this dump is not the one the manifest describes" >&2
        exit 1
    fi
    EXPECTED_SCHEMA="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['schema_version'])" "$MANIFEST")"
    echo "manifest: schema $EXPECTED_SCHEMA, checksum verified"
else
    EXPECTED_SCHEMA=""
    echo "no manifest beside this dump; restoring without a version to check against"
fi

EXISTING="$(psql "$LIBPQ_URL" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]')"
if [ "$EXISTING" != "0" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "target already has $EXISTING tables. Set FORCE=1 if you meant to overwrite it." >&2
    exit 1
fi

echo "restoring $DUMP"
pg_restore --no-owner --no-privileges --clean --if-exists --dbname="$LIBPQ_URL" "$DUMP"

RESTORED_SCHEMA="$(psql "$LIBPQ_URL" -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')"
TABLES="$(psql "$LIBPQ_URL" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]')"

echo "restored: schema $RESTORED_SCHEMA, $TABLES tables"

if [ -n "$EXPECTED_SCHEMA" ] && [ "$RESTORED_SCHEMA" != "$EXPECTED_SCHEMA" ]; then
    echo "schema version mismatch: manifest says $EXPECTED_SCHEMA, database says $RESTORED_SCHEMA" >&2
    exit 1
fi
if [ "$TABLES" = "0" ]; then
    echo "the restore produced no tables" >&2
    exit 1
fi

echo "restore verified"
