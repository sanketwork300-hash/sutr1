"""Generate the event schemas and the /v1 OpenAPI document.

Contracts are generated *into files that are committed*, then checked by
`check_contracts.py`. That is deliberately different from serving a
specification from the running code: a generated-and-committed file shows up in
a diff when it changes, so a breaking change is something a reviewer sees
rather than something a consumer discovers.

Run from `server/`:  uv run python scripts/generate_contracts.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sutr.events.topics import ALL_TOPICS, spec_for  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVENTS_DIR = ROOT / "contracts" / "events"
OPENAPI_DIR = ROOT / "contracts" / "openapi"

# The envelope's required fields (ESDS LLD §5.6). Every event schema embeds
# these, so a consumer can read any event without knowing its type.
ENVELOPE_PROPERTIES = {
    "event_id": {"type": "string", "format": "uuid", "description": "Unique per event."},
    "event_type": {"type": "string", "description": "The fact that occurred."},
    "event_version": {
        "type": "integer",
        "minimum": 1,
        "description": "Payload version for this event type. Changes are additive.",
    },
    "timestamp": {"type": "string", "format": "date-time"},
    "correlation_id": {
        "type": ["string", "null"],
        "description": "Joins every stage of one logical operation.",
    },
    "tenant_id": {
        "type": ["string", "null"],
        "description": "The organization the fact concerns. Null for platform-wide facts.",
    },
    "resource_id": {
        "type": ["string", "null"],
        "description": "The entity the fact concerns; also the partition key source.",
    },
    "producer": {"type": "string", "description": "The component that emitted it."},
    "payload": {"type": "object", "description": "Type-specific detail."},
}

REQUIRED = ["event_id", "event_type", "event_version", "timestamp", "producer", "payload"]


def schema_for(event_type: str) -> dict:
    spec = spec_for(event_type)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://sutr.sh/contracts/events/{event_type}.json",
        "title": event_type,
        "description": (
            f"{spec.description if spec else 'Platform event.'} Immutable fact; never a command."
        ),
        "type": "object",
        "x-partition-key": spec.partition_by if spec else "tenant_id",
        "properties": {
            **ENVELOPE_PROPERTIES,
            "event_type": {**ENVELOPE_PROPERTIES["event_type"], "const": event_type},
        },
        "required": REQUIRED,
        "additionalProperties": False,
    }


def main() -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    OPENAPI_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for event_type in ALL_TOPICS:
        path = EVENTS_DIR / f"{event_type}.json"
        path.write_text(json.dumps(schema_for(event_type), indent=2) + "\n", encoding="utf-8")
        written += 1

    # Remove schemas for event types that no longer exist, so the directory
    # stays a description of the code rather than an archive of it.
    known = {f"{event_type}.json" for event_type in ALL_TOPICS}
    removed = 0
    for path in EVENTS_DIR.glob("*.json"):
        if path.name not in known:
            path.unlink()
            removed += 1

    from sutr.main import app

    spec = app.openapi()
    v1_paths = {path: item for path, item in spec["paths"].items() if path.startswith("/v1/")}
    document = {
        "openapi": spec["openapi"],
        "info": {
            "title": "Sutr platform API",
            "version": "1.0.0",
            "description": (
                "The /v1 surface: standard response envelope, cursor pagination, and the "
                "platform error shape. The /api surface is frozen for backward compatibility "
                "and is described by the server's own /openapi.json (ADR-004)."
            ),
        },
        "paths": v1_paths,
        "components": spec.get("components", {}),
    }
    (OPENAPI_DIR / "platform.yaml").write_text(_to_yaml(document), encoding="utf-8")

    print(f"events: {written} written, {removed} removed")
    print(f"openapi: {len(v1_paths)} /v1 paths")


def _to_yaml(value) -> str:
    """Serialize without a YAML dependency in this script's import path."""
    import yaml

    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=100)


if __name__ == "__main__":
    main()
