"""Verify the committed contracts still describe the code.

Runs in CI. A contract that drifts from the code is worse than no contract: it
tells a consumer something that is no longer true.

Run from `server/`:  uv run python scripts/check_contracts.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sutr.events.topics import ALL_TOPICS  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVENTS_DIR = ROOT / "contracts" / "events"
OPENAPI_DIR = ROOT / "contracts" / "openapi"

REGENERATE = "Regenerate with: uv run python scripts/generate_contracts.py"

REQUIRED_ENVELOPE_FIELDS = {
    "event_id",
    "event_type",
    "event_version",
    "timestamp",
    "correlation_id",
    "tenant_id",
    "resource_id",
    "producer",
    "payload",
}


def check_events() -> list[str]:
    problems: list[str] = []
    if not EVENTS_DIR.exists():
        return [f"contracts/events/ is missing. {REGENERATE}"]

    on_disk = {path.stem for path in EVENTS_DIR.glob("*.json")}
    declared = set(ALL_TOPICS)

    for missing in sorted(declared - on_disk):
        problems.append(f"event type {missing!r} has no schema in contracts/events/. {REGENERATE}")
    for extra in sorted(on_disk - declared):
        problems.append(
            f"contracts/events/{extra}.json describes an event type the code does not "
            f"declare. {REGENERATE}"
        )

    for path in sorted(EVENTS_DIR.glob("*.json")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name} is not valid JSON: {exc}")
            continue
        properties = set(schema.get("properties", {}))
        missing = REQUIRED_ENVELOPE_FIELDS - properties
        if missing:
            problems.append(
                f"{path.name} does not carry the envelope fields {sorted(missing)} — every "
                "event must be readable without knowing its type."
            )
        if schema.get("properties", {}).get("event_type", {}).get("const") != path.stem:
            problems.append(f"{path.name} does not pin `event_type` to {path.stem!r}.")
        if not schema.get("x-partition-key"):
            problems.append(
                f"{path.name} declares no partition key, so ordering for this entity is undefined."
            )
    return problems


def _served_paths(app) -> set[str]:
    """Every path the app serves, walking included routers.

    FastAPI stopped flattening included routers into `app.routes`. Reading
    `app.routes` directly still *works* — it returns mount points — so this
    check went on passing structurally while comparing the contract against
    almost nothing. Walking is version-independent.
    """
    found: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None) or getattr(route, "path_format", None)
            if path:
                found.add(path)
            nested = getattr(route, "original_router", None) or getattr(route, "app", None)
            child = getattr(nested, "routes", None)
            if child:
                walk(child)

    walk(app.routes)
    return found


def check_openapi() -> list[str]:
    problems: list[str] = []
    document_path = OPENAPI_DIR / "platform.yaml"
    if not document_path.exists():
        return [f"contracts/openapi/platform.yaml is missing. {REGENERATE}"]

    import yaml

    try:
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"contracts/openapi/platform.yaml is not valid YAML: {exc}"]

    from sutr.main import app

    served = {path for path in _served_paths(app) if path.startswith("/v1/")}
    documented = set(document.get("paths", {}))

    for missing in sorted(served - documented):
        problems.append(f"{missing} is served but not in the contract. {REGENERATE}")
    for extra in sorted(documented - served):
        problems.append(
            f"{extra} is in the contract but is not served — a promise nothing keeps. {REGENERATE}"
        )
    return problems


def main() -> int:
    problems = check_events() + check_openapi()
    if problems:
        print("Contract check failed:\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Contracts OK: {len(ALL_TOPICS)} event schemas, /v1 OpenAPI in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
