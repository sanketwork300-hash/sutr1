"""Contract tests over every bundled integration.

These guard the invariants the runtime relies on but that nothing previously
verified across the whole catalog: unique well-formed ids, coherent auth
declarations, resolvable URLs (syntactically), valid declarative tool
definitions for custom (REST) integrations, and tool_categories that reference
real tools.
"""

import re
from urllib.parse import urlsplit

import pytest

from agent_port.integrations.registry import _INTEGRATIONS
from agent_port.integrations.types import (
    ApiTool,
    CustomIntegration,
    CustomTool,
    OAuthAuth,
    RemoteMcpIntegration,
    TokenAuth,
)

ALL = sorted(_INTEGRATIONS.values(), key=lambda i: i.id)
IDS = [i.id for i in ALL]

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_PARAM_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_PATH_PARAM_RE = re.compile(r"{([^{}]+)}")
# RFC 7230 header-name token characters (underscores are legal, e.g. DD_API_KEY).
_HEADER_RE = re.compile(r"^[!#$%&'*+\-.^_`|~A-Za-z0-9]+$")
_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def test_catalog_size_and_uniqueness():
    assert len(_INTEGRATIONS) == 49
    assert len(set(IDS)) == 49


@pytest.mark.parametrize("integration", ALL, ids=IDS)
def test_identity_and_metadata(integration):
    assert _ID_RE.match(integration.id), integration.id
    assert not integration.id.startswith(("custom_", "customapi_")), (
        "bundled ids must not collide with the reserved custom prefixes"
    )
    assert integration.name.strip()
    assert integration.description.strip()
    available, reason = integration.is_available()
    assert isinstance(available, bool)
    if not available:
        assert reason


@pytest.mark.parametrize("integration", ALL, ids=IDS)
def test_type_specific_shape(integration):
    if isinstance(integration, RemoteMcpIntegration):
        assert integration.type == "remote_mcp"
        parsed = urlsplit(integration.url)
        assert parsed.scheme == "https", f"{integration.id} must use https"
        assert parsed.hostname
    elif isinstance(integration, CustomIntegration):
        assert integration.type == "custom"
        assert urlsplit(integration.base_url).scheme == "https"
        assert integration.tools, f"{integration.id} declares no tools"
    else:
        pytest.fail(f"Unknown integration class for {integration.id}")


@pytest.mark.parametrize("integration", ALL, ids=IDS)
def test_auth_declarations(integration):
    assert integration.auth is not None
    assert len(integration.auth) >= 1, f"{integration.id} declares no auth methods"
    for auth in integration.auth:
        if isinstance(auth, TokenAuth):
            assert _HEADER_RE.match(auth.header), f"{integration.id}: bad header {auth.header!r}"
            assert "{token}" in auth.format, f"{integration.id}: format missing {{token}}"
        elif isinstance(auth, OAuthAuth):
            if auth.provider:
                # Pre-configured providers must fully specify their endpoints.
                assert auth.authorization_url.startswith("https://"), integration.id
                assert auth.token_url.startswith("https://"), integration.id
            assert auth.scope_param
        else:
            pytest.fail(f"{integration.id} uses unexpected auth type {type(auth).__name__}")


def _custom_integrations() -> list[CustomIntegration]:
    return [i for i in ALL if isinstance(i, CustomIntegration)]


@pytest.mark.parametrize(
    "integration", _custom_integrations(), ids=[i.id for i in _custom_integrations()]
)
def test_custom_tool_definitions(integration):
    seen: set[str] = set()
    for tool in integration.tools:
        assert _TOOL_NAME_RE.match(tool.name), f"{integration.id}.{tool.name}"
        assert tool.name not in seen, f"{integration.id}: duplicate tool {tool.name}"
        seen.add(tool.name)
        assert tool.description.strip(), f"{integration.id}.{tool.name} has no description"

        param_names = [p.name for p in tool.params]
        assert len(param_names) == len(set(param_names)), (
            f"{integration.id}.{tool.name}: duplicate params"
        )
        for p in tool.params:
            assert _PARAM_NAME_RE.match(p.name), f"{integration.id}.{tool.name}.{p.name}"
            assert p.type in _JSON_TYPES, f"{integration.id}.{tool.name}.{p.name}: type {p.type!r}"

        if isinstance(tool, ApiTool):
            assert tool.method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, (
                f"{integration.id}.{tool.name}: {tool.method}"
            )
            assert tool.path.startswith("/"), f"{integration.id}.{tool.name}: {tool.path}"
            declared = set(param_names)
            for placeholder in _PATH_PARAM_RE.findall(tool.path):
                assert placeholder in declared, (
                    f"{integration.id}.{tool.name}: path param {{{placeholder}}} not declared"
                )
        else:
            assert isinstance(tool, CustomTool)
            assert callable(tool.run)


@pytest.mark.parametrize("integration", ALL, ids=IDS)
def test_tool_categories_reference_real_tools_for_custom(integration):
    # For remote MCP integrations the upstream tool list isn't known statically,
    # so categories can only be validated for custom (REST) integrations.
    if not isinstance(integration, CustomIntegration) or not integration.tool_categories:
        return
    tool_names = {t.name for t in integration.tools}
    unknown = set(integration.tool_categories) - tool_names
    assert not unknown, f"{integration.id}: categories for unknown tools {sorted(unknown)}"
