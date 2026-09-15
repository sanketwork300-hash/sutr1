"""Call metrics for a generated MCP server.

This file is a *template*: it is copied verbatim into every generated package
as `sutr_metrics.py` and is never imported by the platform itself. It lives as
a real file rather than a string constant for the same reason
`runtime/request_builder.py` does — code that ships to users should be readable,
lintable and diffable where it is written.

The LLD's generated middleware chain is *Authentication → Validation → Logging
→ Execution → Metrics → Response* (§3.6). This module is the Metrics stage.

It is deliberately in-process and stdlib-only. A generated server is a single
container the provider runs wherever they like, and making it depend on a
metrics backend would make it depend on infrastructure the platform cannot see.
What it does instead is expose the numbers in the Prometheus text exposition
format at `/metrics`, so anything that already scrapes containers can scrape
this one, and nothing has to be configured for the server to start.

Counters are cumulative since process start, which is what a counter means.
Restarting the container resets them, and the scraper is expected to cope with
that — it is the contract every Prometheus client makes.
"""

import threading
import time

# Bucket edges in seconds. Chosen for an HTTP call to somebody else's API:
# tight enough at the bottom to see a fast local endpoint, wide enough at the
# top to see a call that is about to hit the read timeout.
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

_BACKSLASH = chr(92)
_NEWLINE = chr(10)


def _escape(value):
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return (
        str(value)
        .replace(_BACKSLASH, _BACKSLASH * 2)
        .replace('"', _BACKSLASH + '"')
        .replace(_NEWLINE, _BACKSLASH + "n")
    )


class Metrics:
    """Counts and latencies, per tool.

    Every method is safe to call from any thread: the HTTP transports serve
    concurrently, and a torn counter is worse than no counter because it still
    looks like a real number.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self._calls = {}
        self._durations = {}
        self._statuses = {}
        self._retries = 0
        self._refused = 0

    def record(self, tool, outcome, duration_seconds, status_code=None, attempts=1):
        """One completed call, whatever its outcome."""
        with self._lock:
            self._calls[(tool, outcome)] = self._calls.get((tool, outcome), 0) + 1
            total, count, buckets = self._durations.get(tool, (0.0, 0, [0] * len(BUCKETS)))
            total += duration_seconds
            count += 1
            for index, edge in enumerate(BUCKETS):
                if duration_seconds <= edge:
                    buckets[index] += 1
            self._durations[tool] = (total, count, buckets)
            if status_code is not None:
                key = (tool, int(status_code))
                self._statuses[key] = self._statuses.get(key, 0) + 1
            self._retries += max(0, int(attempts) - 1)
            if outcome == "circuit_open":
                self._refused += 1

    def snapshot(self):
        """The same numbers as JSON, for /health and for tests."""
        with self._lock:
            per_tool = {}
            for (tool, outcome), count in sorted(self._calls.items()):
                entry = per_tool.setdefault(tool, {"outcomes": {}, "calls": 0})
                entry["outcomes"][outcome] = count
                entry["calls"] += count
            for tool, (total, count, _buckets) in sorted(self._durations.items()):
                entry = per_tool.setdefault(tool, {"outcomes": {}, "calls": 0})
                entry["duration_seconds_total"] = round(total, 6)
                entry["duration_seconds_avg"] = round(total / count, 6) if count else None
            return {
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "retries": self._retries,
                "circuit_open_refusals": self._refused,
                "tools": per_tool,
            }

    def prometheus_text(self):
        """Prometheus text exposition format, version 0.0.4."""
        with self._lock:
            calls = sorted(self._calls.items())
            statuses = sorted(self._statuses.items())
            durations = sorted(self._durations.items())
            retries = self._retries
            refused = self._refused

        lines = [
            "# HELP mcp_tool_calls_total Tool calls handled by this server.",
            "# TYPE mcp_tool_calls_total counter",
        ]
        for (tool, outcome), count in calls:
            lines.append(
                'mcp_tool_calls_total{tool="%s",outcome="%s"} %d'
                % (_escape(tool), _escape(outcome), count)
            )

        lines.append("# HELP mcp_tool_responses_total Upstream responses by status code.")
        lines.append("# TYPE mcp_tool_responses_total counter")
        for (tool, status), count in statuses:
            lines.append(
                'mcp_tool_responses_total{tool="%s",status="%d"} %d'
                % (_escape(tool), status, count)
            )

        lines.append("# HELP mcp_tool_duration_seconds Wall time of a tool call.")
        lines.append("# TYPE mcp_tool_duration_seconds histogram")
        for tool, (total, count, buckets) in durations:
            for index, edge in enumerate(BUCKETS):
                lines.append(
                    'mcp_tool_duration_seconds_bucket{tool="%s",le="%s"} %d'
                    % (_escape(tool), edge, buckets[index])
                )
            lines.append(
                'mcp_tool_duration_seconds_bucket{tool="%s",le="+Inf"} %d' % (_escape(tool), count)
            )
            lines.append(
                'mcp_tool_duration_seconds_sum{tool="%s"} %s' % (_escape(tool), round(total, 6))
            )
            lines.append('mcp_tool_duration_seconds_count{tool="%s"} %d' % (_escape(tool), count))

        lines.append("# HELP mcp_tool_retries_total Retried attempts beyond the first.")
        lines.append("# TYPE mcp_tool_retries_total counter")
        lines.append("mcp_tool_retries_total %d" % retries)
        lines.append("# HELP mcp_circuit_open_refusals_total Calls refused by an open breaker.")
        lines.append("# TYPE mcp_circuit_open_refusals_total counter")
        lines.append("mcp_circuit_open_refusals_total %d" % refused)
        return _NEWLINE.join(lines) + _NEWLINE


METRICS = Metrics()
