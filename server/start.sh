#!/bin/sh
set -e

# One-time data migration from the pre-rebrand (AgentPort) default SQLite
# path. Only fires when the volume still holds the old file and the new one
# does not exist yet, so it can never clobber data.
if [ "${DATABASE_URL:-}" = "sqlite:////data/sutr.db" ] \
   && [ -f /data/agent_port.db ] && [ ! -f /data/sutr.db ]; then
    echo "Migrating SQLite database: /data/agent_port.db -> /data/sutr.db"
    mv /data/agent_port.db /data/sutr.db
fi

# The image runs as uid 1000 (see Dockerfile). A volume created by an earlier
# version of this image is owned by root, and SQLite's failure mode for that is
# "attempt to write a readonly database" raised from inside Alembic — a
# traceback that says nothing about ownership. Say it here instead, before
# anything else runs.
DATA_DIR="$(dirname "${DATABASE_URL#sqlite:///}")"
case "${DATABASE_URL:-}" in
sqlite*)
    if [ -d "$DATA_DIR" ] && [ ! -w "$DATA_DIR" ]; then
        echo "ERROR: $DATA_DIR is not writable by $(id -un) (uid $(id -u))." >&2
        echo "" >&2
        echo "This image runs as a non-root user. A data volume created by an" >&2
        echo "earlier version is owned by root, and Docker does not re-own an" >&2
        echo "existing volume. Fix it once, from the host:" >&2
        echo "" >&2
        echo "  docker run --rm -v <volume>:/data alpine chown -R 1000:1000 /data" >&2
        echo "" >&2
        echo "then start the container again. Nothing has been changed." >&2
        exit 1
    fi
    ;;
esac

# TRUST_PROXY_HEADERS=true makes uvicorn honour X-Forwarded-For / X-Forwarded-Proto
# so per-IP rate limits and audit IPs see the real client instead of the proxy.
# Only enable when a trusted reverse proxy (e.g. the bundled Caddy) is the sole
# ingress — otherwise clients can spoof their IP.
if [ "${TRUST_PROXY_HEADERS:-false}" = "true" ]; then
    exec uv run uvicorn sutr.main:app --host 0.0.0.0 --port "${PORT:-4747}" \
        --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
fi

exec uv run uvicorn sutr.main:app --host 0.0.0.0 --port "${PORT:-4747}"
