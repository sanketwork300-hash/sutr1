"""The `/v1` API surface (ESDS LLD §5.5, ADR-004).

`/api/...` is frozen: the published CLI and both SDKs read its bare-JSON
bodies, and breaking them to satisfy a style guide would be a poor trade.
`/v1/...` is where the LLD's standards apply — the response envelope, cursor
pagination, `X-Request-ID` / `X-Correlation-ID`, `Idempotency-Key`, and the
standard error shape.

Both surfaces call the same service layer. A `/v1` route is never a second
implementation of an `/api` route; where they overlap, one of them is an
adapter.
"""
