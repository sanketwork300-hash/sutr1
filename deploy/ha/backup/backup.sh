#!/usr/bin/env bash
# Take a backup that can actually be restored.
#
# Custom format (-Fc), because it is the format `pg_restore` can restore
# selectively and in parallel; a plain SQL dump can only be replayed whole.
#
# The manifest written alongside is the part people skip. A backup file whose
# schema version nobody recorded is a file somebody will restore into the wrong
# code and debug for an afternoon.
#
# Usage:
#   DATABASE_URL=postgresql://user:pass@host:5432/sutr ./backup.sh /var/backups/sutr
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL to the database to back up}"
DEST="${1:-./backups}"
mkdir -p "$DEST"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$DEST/sutr-$STAMP.dump"
MANIFEST="$DEST/sutr-$STAMP.manifest.json"

# psql and pg_dump want a libpq URL; SQLAlchemy's driver suffix is not one.
LIBPQ_URL="${DATABASE_URL/+psycopg2/}"
LIBPQ_URL="${LIBPQ_URL/+psycopg/}"

echo "dumping to $DUMP"
pg_dump --format=custom --no-owner --no-privileges --file="$DUMP" "$LIBPQ_URL"

SCHEMA_VERSION="$(psql "$LIBPQ_URL" -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')"
SERVER_VERSION="$(psql "$LIBPQ_URL" -tAc 'SHOW server_version' | tr -d '[:space:]')"
SIZE="$(stat -c %s "$DUMP" 2>/dev/null || stat -f %z "$DUMP")"
SHA="$(sha256sum "$DUMP" | cut -d' ' -f1)"

cat > "$MANIFEST" <<JSON
{
  "file": "$(basename "$DUMP")",
  "taken_at": "$STAMP",
  "bytes": $SIZE,
  "sha256": "$SHA",
  "schema_version": "$SCHEMA_VERSION",
  "postgres_version": "$SERVER_VERSION",
  "format": "pg_dump custom (-Fc)",
  "encrypted": false
}
JSON

echo "manifest: $MANIFEST"
cat "$MANIFEST"

# The LLD asks for encrypted backups (§5.4). This script does not encrypt: the
# key management that makes encryption meaningful — where the key lives, who
# can read it, how it rotates — is a deployment decision, and a script that
# generated its own key and left it next to the backup would be theatre. Pipe
# the dump through age/gpg/KMS on the way to wherever it is stored, and set
# "encrypted" in the manifest accordingly.
