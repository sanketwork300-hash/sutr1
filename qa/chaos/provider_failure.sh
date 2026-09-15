#!/usr/bin/env bash
# Scenario 1: a provider stops answering.
#
# The claim under test (ADR-078): after repeated failures the circuit opens,
# calls fail immediately with a named error instead of occupying a worker for
# the full timeout, and *other* providers keep working.
#
# What makes this a chaos test rather than a unit test is that it runs against
# a live instance over HTTP and measures the wall-clock difference.
set -euo pipefail

BASE_URL="${BASE_URL:?set BASE_URL, e.g. http://localhost:8099}"
API_KEY="${API_KEY:?set API_KEY}"
INTEGRATION="${INTEGRATION:?set INTEGRATION to a custom API integration pointing at an unreachable host}"
TOOL="${TOOL:?set TOOL to one of its tools}"

call() {
    curl -s -o /dev/null -w "%{time_total}" \
        -X POST "$BASE_URL/api/tools/$INTEGRATION/call" \
        -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
        -d "{\"tool_name\":\"$TOOL\",\"args\":{}}"
}

echo "calling a provider that cannot answer"
for i in $(seq 1 6); do
    printf "  attempt %d: %ss\n" "$i" "$(call)"
done

echo
echo "circuit state as the platform sees it:"
curl -s "$BASE_URL/v1/resilience/providers" -H "X-API-Key: $API_KEY" \
    | python3 -m json.tool

echo
echo "Expected: the first attempts take about the connect timeout each, and"
echo "the later ones return in milliseconds — the provider is no longer being"
echo "called at all. The endpoint above should list it as quarantined."
