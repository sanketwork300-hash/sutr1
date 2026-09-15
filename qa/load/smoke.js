// What this instance actually serves, per second, on the machine it is run on.
//
// The LLD's cover targets — 100k invocations/minute, 10k concurrent agents,
// 1M searches/day — are *design* targets. Nothing here proves them, and a
// result from this script must never be presented as though it did: one
// container on one machine with SQLite is not the deployment those numbers
// describe.
//
// What this script is for is narrower and still useful: a number for the paths
// that matter, taken the same way each time, so a change that makes the
// gateway three times slower is noticed by someone other than a customer.
//
//   k6 run -e BASE_URL=http://localhost:8099 -e API_KEY=... qa/load/smoke.js
import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8099";
const API_KEY = __ENV.API_KEY || "";

// Per-endpoint latency, because one aggregate p95 over four endpoints of very
// different cost is a number that describes nothing.
const health = new Trend("sutr_health_ms", true);
const capabilities = new Trend("sutr_capabilities_ms", true);
const telemetry = new Trend("sutr_telemetry_ms", true);
const discovery = new Trend("sutr_discovery_ms", true);

export const options = {
  scenarios: {
    steady: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 20),
      duration: __ENV.DURATION || "30s",
    },
  },
  thresholds: {
    // Deliberately loose. These are regression tripwires, not SLOs — an SLO
    // this repository has not measured is a number it must not publish.
    http_req_failed: ["rate<0.01"],
    sutr_health_ms: ["p(95)<100"],
    sutr_capabilities_ms: ["p(95)<500"],
    sutr_discovery_ms: ["p(95)<2000"],
  },
};

const authenticated = { headers: { "X-API-Key": API_KEY, "Content-Type": "application/json" } };

export default function () {
  // The cheapest possible path: how fast is the process itself?
  const liveness = http.get(`${BASE_URL}/health/live`);
  health.add(liveness.timings.duration);
  check(liveness, { "health 200": (r) => r.status === 200 });

  // Authentication plus the response envelope, no database read of consequence.
  const capability = http.get(`${BASE_URL}/v1/platform/capabilities`, authenticated);
  capabilities.add(capability.timings.duration);
  check(capability, { "capabilities 200": (r) => r.status === 200 });

  // A database read, org-scoped and aggregated.
  const summary = http.get(`${BASE_URL}/v1/observability/summary?window_hours=1`, authenticated);
  telemetry.add(summary.timings.duration);
  check(summary, { "telemetry 200": (r) => r.status === 200 });

  // The LLD's own latency-critical path: intent to ranked tools.
  const search = http.post(
    `${BASE_URL}/v1/discovery/search`,
    JSON.stringify({ intent: "refund a payment for a customer", limit: 5 }),
    authenticated,
  );
  discovery.add(search.timings.duration);
  check(search, { "discovery 200": (r) => r.status === 200 });
}
