"""Standalone MCP server package generation: content, determinism, and — the
real proof — the generated code actually runs and its generated tests pass."""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from sutr.integrations.types import ApiTool, Param
from sutr.openapi.packaging import build_server_package, env_var_for, slugify

TOOLS = [
    ApiTool(
        name="list_pets",
        description="List pets. HTTP GET /pets.",
        method="GET",
        path="/pets",
        params=[Param(name="limit", type="integer", location="query")],
    ),
    ApiTool(
        name="get_pet",
        description="Fetch one pet. HTTP GET /pets/{pet_id}.",
        method="GET",
        path="/pets/{pet_id}",
        params=[
            Param(name="pet_id", type="string", required=True, location="path"),
            Param(name="X_Trace_Id", type="string", location="header", wire_name="X-Trace-Id"),
        ],
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

EXPECTED_FILES = {
    ".env.example",
    "Dockerfile",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "server.py",
    "sutr_runtime.py",
    "test_server.py",
    "tools.json",
}


def _build(**overrides) -> tuple[str, bytes]:
    kwargs = dict(
        name="Petstore Tools",
        base_url="https://api.petstore.example.com/v1",
        token_header="X-Api-Key",
        token_format="{token}",
        tools=TOOLS,
        api_title="Petstore",
        api_version="1.2.0",
    )
    kwargs.update(overrides)
    return build_server_package(**kwargs)


@pytest.fixture(name="extracted")
def extracted_fixture(tmp_path) -> Path:
    _, data = _build()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(tmp_path)
    return tmp_path


def test_package_contains_expected_files():
    filename, data = _build()
    assert filename == "petstore_tools-mcp-server.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == EXPECTED_FILES


def test_package_is_deterministic():
    assert _build()[1] == _build()[1]


def test_bundle_data_and_readme(extracted):
    bundle = json.loads((extracted / "tools.json").read_text(encoding="utf-8"))
    assert bundle["base_url"] == "https://api.petstore.example.com/v1"
    assert bundle["auth"]["token_header"] == "X-Api-Key"
    assert bundle["auth"]["env_var"] == "PETSTORE_TOOLS_API_TOKEN"
    assert {t["name"] for t in bundle["tools"]} == {"list_pets", "get_pet", "create_pet"}

    readme = (extracted / "README.md").read_text(encoding="utf-8")
    assert "`list_pets`" in readme
    assert "PETSTORE_TOOLS_API_TOKEN" in readme
    env = (extracted / ".env.example").read_text(encoding="utf-8")
    assert "PETSTORE_TOOLS_API_TOKEN=" in env


def test_no_auth_package_omits_env_var():
    _, data = _build(token_header="", token_format="")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        bundle = json.loads(archive.read("tools.json"))
        env = archive.read(".env.example").decode()
    assert bundle["auth"]["env_var"] is None
    assert "no credentials" in env


def test_empty_toolset_refused():
    with pytest.raises(ValueError):
        _build(tools=[])


def test_slug_and_env_var_sanitization():
    assert slugify("Höla / API v2!") == "h_la_api_v2"
    assert env_var_for("h_la_api_v2") == "H_LA_API_V2_API_TOKEN"


def test_generated_runtime_builds_correct_requests(extracted, monkeypatch):
    """Import the generated runtime and verify request semantics directly."""
    monkeypatch.syspath_prepend(str(extracted))
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_runtime", extracted / "sutr_runtime.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)

    bundle = rt.load_bundle(extracted / "tools.json")
    get_pet = next(t for t in bundle["tools"] if t["name"] == "get_pet")
    req = rt.build_request(
        bundle,
        get_pet,
        {"pet_id": "a/b c", "X_Trace_Id": "trace-9"},
        token="sk_secret",
    )
    assert req["url"] == "https://api.petstore.example.com/v1/pets/a%2Fb%20c"
    assert req["headers"]["X-Trace-Id"] == "trace-9"  # wire name, not arg name
    assert req["headers"]["X-Api-Key"] == "sk_secret"
    assert req["json_body"] is None  # GET

    create = next(t for t in bundle["tools"] if t["name"] == "create_pet")
    req = rt.build_request(bundle, create, {"name": "Rex", "tag": None})
    assert req["json_body"] == {"name": "Rex"}  # None values dropped
    assert "X-Api-Key" not in req["headers"]  # no token supplied

    schema = rt.input_schema(get_pet)
    assert schema["required"] == ["pet_id"]


def test_generated_server_lists_tools(extracted):
    """The generated server.py runs (imports mcp, loads the bundle)."""
    proc = subprocess.run(
        [sys.executable, "server.py", "--list-tools"],
        cwd=extracted,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "list_pets" in proc.stdout
    assert "GET /pets/{pet_id}" in proc.stdout


def test_generated_tests_pass(extracted):
    """The package's own generated test suite passes offline."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_server.py", "-q", "-p", "no:cacheprovider"],
        cwd=extracted,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "passed" in proc.stdout


def test_generated_server_serves_http_health(extracted):
    """--transport http boots and serves the /health probe (used by the
    deployment engine's providers for monitoring)."""
    import socket
    import time
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    proc = subprocess.Popen(
        [
            sys.executable,
            "server.py",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=extracted,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        body = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    body = json.loads(r.read())
                    break
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError(proc.stdout.read().decode(errors="replace"))
                time.sleep(0.3)
        assert body is not None, "server never became healthy"
        assert body["status"] == "ok"
        assert body["tools"] == 3
    finally:
        proc.kill()
        proc.wait(timeout=10)


async def test_package_endpoint_returns_zip_and_audits(client, session, test_org):
    from tests.test_api.test_openapi_projects import PETSTORE

    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]

    resp = await client.post(
        f"/api/openapi/{project_id}/package",
        json={"filters": {"exclude_tags": ["admin"]}, "integration_name": "Petstore Kit"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="petstore_kit-mcp-server.zip"' in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert set(archive.namelist()) == EXPECTED_FILES
        bundle = json.loads(archive.read("tools.json"))
    assert {t["name"] for t in bundle["tools"]} == {"list_pets", "create_pet"}

    from sqlmodel import select

    from sutr.models.audit_event import AuditEvent

    session.expire_all()
    event = session.exec(
        select(AuditEvent).where(AuditEvent.action == "openapi.package_generated")
    ).one()
    assert json.loads(event.metadata_json)["tool_count"] == 2


async def test_package_endpoint_requires_permission(client, session, test_user, test_org):
    from sutr.models.org_membership import OrgMembership
    from tests.test_api.test_openapi_projects import PETSTORE

    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]

    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = "viewer"
    session.add(membership)
    session.commit()

    resp = await client.post(f"/api/openapi/{project_id}/package", json={})
    assert resp.status_code == 403
