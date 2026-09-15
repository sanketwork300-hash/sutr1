# Contracts

The API and event contracts the platform commits to, kept as files rather than
derived from code at runtime.

The distinction matters. A specification generated from the current code always
matches the current code, which makes it useless as a promise: it cannot tell
you that a change broke a consumer, because it changes with the code. A
contract in a file can, and CI checks that the code still satisfies it.

```
contracts/
├── openapi/    one document per service surface (LLD §5.5, build prompt §74)
└── events/     one schema per event type       (LLD §5.6, build prompt §75)
```

## What is here, and what is not

`openapi/` currently describes the **`/v1` surface only**. `/api/...` is
described by FastAPI's generated document at `/openapi.json`, and is frozen by
the compatibility promise in ADR-004 rather than by a contract file — it is
consumed by an already-published CLI and two SDKs, so its shape is fixed by
what is deployed, not by what a file says.

`events/` describes every event type in `sutr/events/topics.py`, including
those nothing emits yet. The names become a contract the moment the first one
is published, so they are written down before that happens rather than after.

Schemas are JSON Schema. The LLD prefers Avro, registered in Apicurio
(§5.6). That needs a running registry to validate against, so it is recorded as
a known limitation rather than guessed at — the envelope carries
`event_version`, so moving to Avro later is a serialization change, not a
contract change.

## Validation

`server/scripts/check_contracts.py` verifies:

- every event type in the code has a schema file, and every schema file names a
  real event type;
- every schema is valid JSON Schema and requires the envelope's mandatory
  fields;
- the `/v1` OpenAPI document parses and covers the routes the server serves.

It runs in CI (`.github/workflows/test.yml`) and locally:

```sh
cd server && uv run python scripts/check_contracts.py
```
