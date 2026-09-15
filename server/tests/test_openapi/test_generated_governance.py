"""Generated-server governance and transports (build prompt §29/§40, ADR-007).

The rule ADR-007 exists to enforce: a generated server running on someone
else's infrastructure does **not** inherit Sutr's approval policies, and must
say so rather than implying otherwise. `standalone` states its independence;
`platform` actually validates a signed access pass, and refuses to start if it
cannot.
"""

import importlib.util
import io
import time
import zipfile

import jwt
import pytest

from sutr.integrations.types import ApiTool, Param
from sutr.openapi.packaging import build_server_package

ISSUER = "https://sutr.example.com"
AUDIENCE = "https://runtime.example.com/mcp"
SECRET = "a-shared-secret-at-least-32-characters-long"

TOOLS = [
    ApiTool(
        name="list_pets",
        description="List pets.",
        method="GET",
        path="/pets",
        params=[Param(name="limit", type="integer", location="query")],
    ),
    ApiTool(
        name="delete_pet",
        description="Delete a pet.",
        method="DELETE",
        path="/pets/{pet_id}",
        params=[Param(name="pet_id", required=True, location="path")],
    ),
]


@pytest.fixture(name="generated")
def generated_fixture(tmp_path):
    _, package = build_server_package(
        name="Petstore",
        base_url="https://api.petstore.example.com/v1",
        token_header="X-Api-Key",
        token_format="{token}",
        tools=TOOLS,
        api_title="Petstore",
        api_version="1.0.0",
    )
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        archive.extractall(tmp_path)
    return tmp_path


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="governance")
def governance_fixture(generated):
    return _load(generated / "sutr_governance.py", "gen_governance")


def _pass(gov_module, **overrides):
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "agent-1",
        "tenant_id": "org-1",
        "tools": ["list_pets"],
        "nonce": f"n-{time.time_ns()}",
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _platform_env(**overrides):
    env = {
        "GOVERNANCE_MODE": "platform",
        "ACCESS_PASS_ISSUER": ISSUER,
        "ACCESS_PASS_AUDIENCE": AUDIENCE,
        "ACCESS_PASS_SECRET": SECRET,
    }
    env.update(overrides)
    return env


# ── Standalone: independent, and says so ─────────────────────────────────────


def test_standalone_is_the_default_and_states_its_independence(governance):
    config = governance.load(env={})
    assert config.mode == governance.STANDALONE
    assert config.enforced is False
    described = config.describe()
    assert "independent" in described
    assert "Anyone who can reach this endpoint" in described


def test_standalone_validates_nothing_because_it_claims_nothing(governance):
    assert governance.load(env={}).validate("anything", "list_pets") is None


def test_an_unknown_mode_is_refused_rather_than_defaulted(governance):
    with pytest.raises(governance.ConfigurationError, match="GOVERNANCE_MODE"):
        governance.load(env={"GOVERNANCE_MODE": "enforced-ish"})


# ── Platform: refuses to start unconfigured ──────────────────────────────────


@pytest.mark.parametrize(
    "missing",
    ["ACCESS_PASS_ISSUER", "ACCESS_PASS_AUDIENCE", "ACCESS_PASS_SECRET"],
)
def test_platform_mode_refuses_to_start_without_its_validation_material(governance, missing):
    env = _platform_env()
    del env[missing]
    with pytest.raises(governance.ConfigurationError) as excinfo:
        governance.load(env=env)
    assert "Refusing to start" in str(excinfo.value)


def test_the_refusal_names_every_missing_value_at_once(governance):
    with pytest.raises(governance.ConfigurationError) as excinfo:
        governance.load(env={"GOVERNANCE_MODE": "platform"})
    message = str(excinfo.value)
    assert "ACCESS_PASS_ISSUER" in message
    assert "ACCESS_PASS_AUDIENCE" in message
    assert "ACCESS_PASS_SECRET" in message


def test_platform_mode_describes_itself_as_enforcing(governance):
    config = governance.load(env=_platform_env())
    assert config.enforced is True
    assert "signed access pass" in config.describe()


# ── Platform: pass validation ────────────────────────────────────────────────


def test_a_valid_pass_is_accepted(governance):
    config = governance.load(env=_platform_env())
    claims = config.validate(_pass(governance), "list_pets")
    assert claims["tenant_id"] == "org-1"


def test_a_missing_pass_is_refused(governance):
    config = governance.load(env=_platform_env())
    with pytest.raises(governance.GovernanceError, match="required"):
        config.validate("", "list_pets")


def test_a_pass_signed_with_the_wrong_key_is_refused(governance):
    config = governance.load(env=_platform_env())
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "agent-1",
            "tenant_id": "org-1",
            "nonce": "n-forged",
            "exp": int(time.time()) + 300,
        },
        "a-different-secret-entirely-000000",
        algorithm="HS256",
    )
    with pytest.raises(governance.GovernanceError):
        config.validate(forged, "list_pets")


def test_an_expired_pass_is_refused(governance):
    config = governance.load(env=_platform_env())
    with pytest.raises(governance.GovernanceError):
        config.validate(_pass(governance, exp=int(time.time()) - 5), "list_pets")


def test_a_pass_for_another_issuer_is_refused(governance):
    config = governance.load(env=_platform_env())
    with pytest.raises(governance.GovernanceError):
        config.validate(_pass(governance, iss="https://evil.example.com"), "list_pets")


def test_a_pass_for_another_audience_is_refused(governance):
    config = governance.load(env=_platform_env())
    with pytest.raises(governance.GovernanceError):
        config.validate(_pass(governance, aud="https://other.example.com"), "list_pets")


def test_a_pass_is_single_use(governance):
    config = governance.load(env=_platform_env())
    token = _pass(governance)
    assert config.validate(token, "list_pets")
    with pytest.raises(governance.GovernanceError, match="already been used"):
        config.validate(token, "list_pets")


def test_a_pass_without_a_nonce_is_refused_because_it_could_be_replayed(governance):
    config = governance.load(env=_platform_env())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "agent-1",
            "tenant_id": "org-1",
            "exp": int(time.time()) + 300,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(governance.GovernanceError, match="nonce"):
        config.validate(token, "list_pets")


def test_a_pass_without_a_tenant_is_refused(governance):
    config = governance.load(env=_platform_env())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "agent-1",
            "nonce": "n-no-tenant",
            "exp": int(time.time()) + 300,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(governance.GovernanceError, match="tenant"):
        config.validate(token, "list_pets")


def test_a_pass_is_scoped_to_the_tools_it_names(governance):
    config = governance.load(env=_platform_env())
    with pytest.raises(governance.GovernanceError, match="delete_pet"):
        config.validate(_pass(governance), "delete_pet")


def test_a_pass_with_no_tool_list_covers_every_tool(governance):
    """Absent `tools` means unscoped; an empty list would mean "nothing"."""
    config = governance.load(env=_platform_env())
    payload = jwt.decode(
        _pass(governance), SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER
    )
    payload.pop("tools")
    payload["nonce"] = "n-unscoped"
    unscoped = jwt.encode(payload, SECRET, algorithm="HS256")
    assert config.validate(unscoped, "delete_pet")


def test_expired_nonces_do_not_accumulate_forever(governance):
    config = governance.load(env=_platform_env())
    cache = config._nonces
    cache.claim("old", time.time() - 1)
    cache.claim("fresh", time.time() + 300)
    assert "old" not in cache._seen
    assert "fresh" in cache._seen


# ── The generated files say the right things ─────────────────────────────────


def test_the_env_example_documents_both_modes(generated):
    text = (generated / ".env.example").read_text(encoding="utf-8")
    assert "GOVERNANCE_MODE=standalone" in text
    assert "independent of Sutr's approval" in text
    assert "ACCESS_PASS_ISSUER" in text


def test_the_server_offers_all_three_transports(generated):
    source = (generated / "server.py").read_text(encoding="utf-8")
    assert '"stdio", "http", "sse"' in source
    assert "SseServerTransport" in source
    assert "StreamableHTTPSessionManager" in source


def test_platform_mode_is_refused_on_stdio_because_there_is_no_per_call_pass(generated):
    source = (generated / "server.py").read_text(encoding="utf-8")
    assert "needs an HTTP transport" in source


def test_the_governance_module_ships_with_the_package(generated):
    assert (generated / "sutr_governance.py").exists()
    assert "pyjwt" in (generated / "requirements.txt").read_text(encoding="utf-8")
