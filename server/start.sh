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

# TRUST_PROXY_HEADERS=true makes uvicorn honour X-Forwarded-For / X-Forwarded-Proto
# so per-IP rate limits and audit IPs see the real client instead of the proxy.
# Only enable when a trusted reverse proxy (e.g. the bundled Caddy) is the sole
# ingress — otherwise clients can spoof their IP.
if [ "${TRUST_PROXY_HEADERS:-false}" = "true" ]; then
    exec uv run uvicorn sutr.main:app --host 0.0.0.0 --port "${PORT:-4747}" \
        --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
fi

exec uv run uvicorn sutr.main:app --host 0.0.0.0 --port "${PORT:-4747}"
