"""Shared fixtures for the MCP generation tests."""

import json

import pytest

from sutr.config import settings
from sutr.integrations.types import ApiTool, Param
from tests.test_api.test_openapi_projects import PETSTORE

TOOLS = [
    ApiTool(
        name="list_pets",
        description="List pets. HTTP GET /pets.",
        method="GET",
        path="/pets",
        params=[Param(name="limit", type="integer", location="query")],
    ),
    ApiTool(
        name="create_pet",
        description="Create a pet. HTTP POST /pets.",
        method="POST",
        path="/pets",
        params=[
            Param(name="name", type="string", required=True, location="body"),
            Param(name="tag", type="string", location="body"),
        ],
    ),
]


@pytest.fixture(autouse=True)
def _generation_settings(monkeypatch):
    """A known configuration, so a test never depends on the developer's env.

    The generated package's own pytest run is off by default: it is a
    subprocess per generation and would dominate the suite's runtime. One test
    turns it on deliberately, which is what keeps that path exercised.
    """
    monkeypatch.setattr(settings, "generation_run_generated_tests", False)
    monkeypatch.setattr(settings, "generation_security_scan_command", "")
    monkeypatch.setattr(settings, "generation_vulnerability_scan_command", "")
    monkeypatch.setattr(settings, "generation_signing_key", "")
    monkeypatch.setattr(settings, "generation_signing_key_id", "")


async def import_petstore(client) -> str:
    """Import the shared Petstore spec and return the project id."""
    response = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def build_files(**overrides) -> dict[str, str]:
    from sutr.openapi.packaging import build_server_files

    kwargs = {
        "name": "Petstore Kit",
        "base_url": "https://api.petstore.example.com/v1",
        "token_header": "X-Api-Key",
        "token_format": "{token}",
        "tools": TOOLS,
        "api_title": "Petstore",
        "api_version": "1.2.0",
    }
    kwargs.update(overrides)
    return build_server_files(**kwargs)


def build_manifest(files: dict[str, str], **overrides) -> dict:
    from sutr.generation.manifest import build_manifest as build

    kwargs = {
        "files": files,
        "bundle": json.loads(files["tools.json"]),
        "runtime": "python",
        "template_version": "2",
        "ir_version": 2,
        "ir_hash": "a" * 64,
        "knowledge_hash": "",
    }
    kwargs.update(overrides)
    return build(**kwargs)
