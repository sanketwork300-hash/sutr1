"""$ref resolution: cycles, depth bombs, expansion bombs, external refs."""

import pytest

from agent_port.openapi.errors import OpenAPIError
from agent_port.openapi.resolver import resolve_refs


def test_simple_ref_inlined():
    doc = {
        "components": {
            "schemas": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}}}
        },
        "paths": {
            "/pets": {
                "get": {"responses": {"200": {"schema": {"$ref": "#/components/schemas/Pet"}}}}
            }
        },
    }
    resolved = resolve_refs(doc)
    schema = resolved["paths"]["/pets"]["get"]["responses"]["200"]["schema"]
    assert schema["type"] == "object"
    assert "name" in schema["properties"]


def test_nested_refs():
    doc = {
        "components": {
            "schemas": {
                "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "string"},
            }
        }
    }
    resolved = resolve_refs(doc)
    assert resolved["components"]["schemas"]["A"]["properties"]["b"]["type"] == "string"


def test_direct_cycle_terminates():
    doc = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/components/schemas/Node"}},
                }
            }
        }
    }
    resolved = resolve_refs(doc)
    placeholder = resolved["components"]["schemas"]["Node"]["properties"]["next"]
    assert placeholder["x-recursive-ref"] == "#/components/schemas/Node"


def test_indirect_cycle_a_b_c_a_terminates():
    doc = {
        "components": {
            "schemas": {
                "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"properties": {"c": {"$ref": "#/components/schemas/C"}}},
                "C": {"properties": {"a": {"$ref": "#/components/schemas/A"}}},
            }
        }
    }
    resolved = resolve_refs(doc)
    a = resolved["components"]["schemas"]["A"]
    c = a["properties"]["b"]["properties"]["c"]
    assert c["properties"]["a"]["x-recursive-ref"] == "#/components/schemas/A"


def test_diamond_is_not_a_cycle():
    doc = {
        "components": {
            "schemas": {
                "Shared": {"type": "string"},
                "Wrap": {
                    "properties": {
                        "x": {"$ref": "#/components/schemas/Shared"},
                        "y": {"$ref": "#/components/schemas/Shared"},
                    }
                },
            }
        }
    }
    resolved = resolve_refs(doc)
    wrap = resolved["components"]["schemas"]["Wrap"]
    assert wrap["properties"]["x"]["type"] == "string"
    assert wrap["properties"]["y"]["type"] == "string"


def test_external_ref_rejected():
    doc = {"a": {"$ref": "https://evil.example.com/spec.json#/x"}}
    with pytest.raises(OpenAPIError) as exc:
        resolve_refs(doc)
    assert exc.value.code == "external_ref"


def test_file_ref_rejected():
    doc = {"a": {"$ref": "other.yaml#/components/schemas/X"}}
    with pytest.raises(OpenAPIError) as exc:
        resolve_refs(doc)
    assert exc.value.code == "external_ref"


def test_broken_ref_rejected():
    doc = {"a": {"$ref": "#/components/schemas/Missing"}}
    with pytest.raises(OpenAPIError) as exc:
        resolve_refs(doc)
    assert exc.value.code == "broken_ref"


def test_expansion_bomb_bounded():
    # Each level references the next twice: 2^40 nodes if fully expanded.
    schemas = {}
    for i in range(40):
        schemas[f"L{i}"] = {
            "properties": {
                "a": {"$ref": f"#/components/schemas/L{i + 1}"},
                "b": {"$ref": f"#/components/schemas/L{i + 1}"},
            }
        }
    schemas["L40"] = {"type": "string"}
    doc = {"components": {"schemas": schemas}, "root": {"$ref": "#/components/schemas/L0"}}
    with pytest.raises(OpenAPIError) as exc:
        resolve_refs(doc)
    assert exc.value.code in ("expansion_too_large", "ref_depth_exceeded")


def test_ref_sibling_description_wins():
    doc = {
        "components": {"schemas": {"X": {"type": "string", "description": "original"}}},
        "field": {"$ref": "#/components/schemas/X", "description": "override"},
    }
    resolved = resolve_refs(doc)
    assert resolved["field"]["description"] == "override"
