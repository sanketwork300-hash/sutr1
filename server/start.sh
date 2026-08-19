#!/bin/sh
set -e

# TRUST_PROXY_HEADERS=true makes uvicorn honour X-Forwarded-For / X-Forwarded-Proto
# so per-IP rate limits and audit IPs see the real client instead of the proxy.
# Only enable when a trusted reverse proxy (e.g. the bundled Caddy) is the sole
# ingress — otherwise clients can spoof their IP.
if [ "${TRUST_PROXY_HEADERS:-false}" = "true" ]; then
    exec uv run uvicorn agent_port.main:app --host 0.0.0.0 --port "${PORT:-4747}" \
        --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
fi

exec uv run uvicorn agent_port.main:app --host 0.0.0.0 --port "${PORT:-4747}"
