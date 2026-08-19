"""Sutr observability: Prometheus metrics and optional OpenTelemetry tracing.

Both are process-level and aggregate. Per-tenant numbers deliberately do NOT
appear in metrics or spans (see metrics.py for why) — they live in the usage
ledger (`models/usage_event.py`) and are read through `/api/usage`, which is
org-scoped and permission-checked.
"""
