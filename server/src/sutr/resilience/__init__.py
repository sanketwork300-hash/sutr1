"""Failure handling: the five patterns LLD §5.8 names, and the taxonomy for the rest.

- `breaker.py`   — circuit breaker and provider health scoring
- `retry.py`     — retry with backoff, and which failures are worth retrying
- `timeouts.py`  — connect, read and overall, on every remote call
- `domains.py`   — the failure domains and severities an incident is classified by
"""
