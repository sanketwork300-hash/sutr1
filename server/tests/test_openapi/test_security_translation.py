"""Complete security translation (build prompt §24, ADR-009).

The rule being enforced: every scheme a specification declares is either
translated into something the runtime can actually send, or reported as
unusable with the reason. Nothing degrades silently — least of all OAuth2 into
"paste a token".
"""

from sutr.openapi.normalizer import ApiDefinition, SecurityScheme, normalize
from sutr.openapi.security import (
    NEEDS_AUTHORIZATION_CODE,
    NEEDS_BASIC,
    NEEDS_CLIENT_CREDENTIALS,
    NEEDS_SECRET,
    UNSUPPORTED,
    translate_security,
)
from sutr.runtime import request_builder as rb


def _spec(schemes: dict, security=None, version: str = "3.0.3") -> dict:
    return {
        "openapi": version,
        "info": {"title": "Secured", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "components": {"securitySchemes": schemes},
        "security": security if security is not None else [{name: [] for name in schemes}],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List things.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _translate(schemes: dict, security=None, version: str = "3.0.3"):
    return translate_security(normalize(_spec(schemes, security, version)))


def _by_name(auth, name):
    return next(p for p in auth.placements if p.scheme_name == name)


# ── API keys in all three locations ──────────────────────────────────────────


def test_api_key_in_a_header():
    auth = _translate({"k": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}})
    placement = _by_name(auth, "k")
    assert (placement.location, placement.name, placement.format) == (
        "header",
        "X-Api-Key",
        "{token}",
    )
    assert placement.requires == NEEDS_SECRET
    assert auth.token_header == "X-Api-Key"


def test_api_key_in_a_query_parameter():
    auth = _translate({"k": {"type": "apiKey", "in": "query", "name": "api_key"}})
    placement = _by_name(auth, "k")
    assert (placement.location, placement.name) == ("query", "api_key")
    assert placement.usable
    assert "query parameter" in placement.credential_hint


def test_api_key_in_a_cookie():
    auth = _translate({"k": {"type": "apiKey", "in": "cookie", "name": "session"}})
    placement = _by_name(auth, "k")
    assert (placement.location, placement.name) == ("cookie", "session")
    assert placement.usable


def test_api_key_with_no_name_is_unusable_and_says_why():
    """The meta-schema rejects this too, but a scheme can reach translation
    from a stored IR, so the translator must refuse it on its own."""
    definition = ApiDefinition(
        title="t",
        version="1",
        openapi_version="3.0.3",
        security_schemes=[SecurityScheme(name="k", type="apiKey", location="header")],
        global_security=["k"],
    )
    auth = translate_security(definition)
    placement = _by_name(auth, "k")
    assert placement.requires == UNSUPPORTED
    assert any(w.code == "unsupported_security_scheme" for w in auth.warnings)


# ── HTTP schemes ─────────────────────────────────────────────────────────────


def test_bearer_and_basic():
    auth = _translate(
        {"b": {"type": "http", "scheme": "bearer"}, "ba": {"type": "http", "scheme": "basic"}}
    )
    assert _by_name(auth, "b").format == "Bearer {token}"
    assert _by_name(auth, "b").requires == NEEDS_SECRET
    assert _by_name(auth, "ba").format == "Basic {token}"
    assert _by_name(auth, "ba").requires == NEEDS_BASIC


def test_an_exotic_http_scheme_is_refused_with_a_reason():
    auth = _translate({"d": {"type": "http", "scheme": "digest"}})
    placement = _by_name(auth, "d")
    assert placement.requires == UNSUPPORTED
    assert "digest" in placement.credential_hint


# ── OAuth2: the grant is kept, not flattened ─────────────────────────────────


def test_client_credentials_is_run_by_the_platform():
    auth = _translate(
        {
            "oauth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/token",
                        "scopes": {"read": "read", "write": "write"},
                    }
                },
            }
        }
    )
    placement = _by_name(auth, "oauth")
    assert placement.requires == NEEDS_CLIENT_CREDENTIALS
    assert placement.flow.kind == "clientCredentials"
    assert placement.flow.token_url == "https://auth.example.com/token"
    assert placement.flow.scopes == ["read", "write"]
    assert "client id and secret" in placement.credential_hint


def test_authorization_code_uses_connected_accounts():
    auth = _translate(
        {
            "oauth": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {
                        "authorizationUrl": "https://auth.example.com/authorize",
                        "tokenUrl": "https://auth.example.com/token",
                        "scopes": {},
                    }
                },
            }
        }
    )
    placement = _by_name(auth, "oauth")
    assert placement.requires == NEEDS_AUTHORIZATION_CODE
    assert placement.flow.authorization_url == "https://auth.example.com/authorize"


def test_client_credentials_is_preferred_over_authorization_code():
    """It is the only grant that completes without a human, so an agent-facing
    tool should reach for it first."""
    auth = _translate(
        {
            "oauth": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {
                        "authorizationUrl": "https://a.example.com/authorize",
                        "tokenUrl": "https://a.example.com/token",
                        "scopes": {},
                    },
                    "clientCredentials": {
                        "tokenUrl": "https://a.example.com/token",
                        "scopes": {},
                    },
                },
            }
        }
    )
    assert _by_name(auth, "oauth").flow.kind == "clientCredentials"


def test_implicit_and_password_grants_are_refused_rather_than_faked():
    grants = {
        "implicit": {"authorizationUrl": "https://a.example.com/authorize", "scopes": {}},
        "password": {"tokenUrl": "https://a.example.com/token", "scopes": {}},
    }
    for grant, flow in grants.items():
        auth = _translate({"oauth": {"type": "oauth2", "flows": {grant: flow}}})
        placement = _by_name(auth, "oauth")
        assert placement.requires == UNSUPPORTED, grant
        assert grant in placement.credential_hint


def test_openid_connect_records_its_discovery_url():
    auth = _translate(
        {
            "oidc": {
                "type": "openIdConnect",
                "openIdConnectUrl": "https://id.example.com/.well-known/openid-configuration",
            }
        }
    )
    placement = _by_name(auth, "oidc")
    assert placement.requires == NEEDS_AUTHORIZATION_CODE
    assert placement.openid_connect_url.endswith("openid-configuration")


# ── mTLS ─────────────────────────────────────────────────────────────────────


def test_mutual_tls_is_reported_as_unsupported_with_an_actionable_reason():
    # mutualTLS was introduced in OpenAPI 3.1.
    auth = _translate({"mtls": {"type": "mutualTLS"}}, version="3.1.0")
    placement = _by_name(auth, "mtls")
    assert placement.requires == UNSUPPORTED
    assert "client certificate" in placement.credential_hint
    assert "standalone generated server" in placement.credential_hint


# ── Several schemes at once ──────────────────────────────────────────────────


def test_every_declared_scheme_is_translated_not_only_the_chosen_one():
    auth = _translate(
        {
            "hdr": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "qry": {"type": "apiKey", "in": "query", "name": "api_key"},
            "bearer": {"type": "http", "scheme": "bearer"},
        }
    )
    assert {p.scheme_name for p in auth.placements} == {"hdr", "qry", "bearer"}
    assert len(auth.usable_placements) == 3


def test_schemes_required_together_are_recorded():
    auth = _translate(
        {
            "hdr": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "qry": {"type": "apiKey", "in": "query", "name": "tenant"},
        },
        security=[{"hdr": [], "qry": []}],
    )
    assert set(auth.required_together) == {"hdr", "qry"}


def test_the_global_security_requirement_picks_the_primary():
    auth = _translate(
        {
            "first": {"type": "apiKey", "in": "header", "name": "X-First"},
            "second": {"type": "apiKey", "in": "header", "name": "X-Second"},
        },
        security=[{"second": []}],
    )
    assert auth.scheme_name == "second"
    assert auth.token_header == "X-Second"


# ── The runtime actually places them ─────────────────────────────────────────


def test_credentials_reach_the_header_query_and_cookie():
    tool = {
        "name": "t",
        "method": "GET",
        "path": "/things",
        "params": [],
        "body_param": None,
        "body_encoding": "none",
    }
    request = rb.build_request(
        "https://api.example.com",
        tool,
        {},
        credentials=[
            {"location": "header", "name": "X-Api-Key", "format": "{token}", "value": "hk"},
            {"location": "query", "name": "api_key", "format": "{token}", "value": "qk"},
            {"location": "cookie", "name": "session", "format": "{token}", "value": "ck"},
            {
                "location": "header",
                "name": "Authorization",
                "format": "Bearer {token}",
                "value": "t",
            },
        ],
    )
    assert request["headers"]["X-Api-Key"] == "hk"
    assert request["headers"]["Authorization"] == "Bearer t"
    assert request["query"]["api_key"] == "qk"
    assert request["headers"]["Cookie"] == "session=ck"


def test_several_cookie_credentials_share_one_cookie_header():
    tool = {"name": "t", "method": "GET", "path": "/x", "params": [], "body_encoding": "none"}
    request = rb.build_request(
        "https://api.example.com",
        tool,
        {},
        credentials=[
            {"location": "cookie", "name": "a", "format": "{token}", "value": "1"},
            {"location": "cookie", "name": "b", "format": "{token}", "value": "2"},
        ],
    )
    assert request["headers"]["Cookie"] == "a=1; b=2"


def test_a_credential_beats_an_argument_that_targets_the_same_place():
    tool = {
        "name": "t",
        "method": "GET",
        "path": "/x",
        "params": [
            {"name": "k", "location": "query", "wire_name": "api_key"},
            {"name": "h", "location": "header", "wire_name": "X-Api-Key"},
        ],
        "body_encoding": "none",
    }
    request = rb.build_request(
        "https://api.example.com",
        tool,
        {"k": "attacker", "h": "attacker"},
        credentials=[
            {"location": "query", "name": "api_key", "format": "{token}", "value": "real"},
            {"location": "header", "name": "X-Api-Key", "format": "{token}", "value": "real"},
        ],
    )
    assert request["query"]["api_key"] == "real"
    assert request["headers"]["X-Api-Key"] == "real"


def test_a_credential_containing_a_newline_is_refused():
    tool = {"name": "t", "method": "GET", "path": "/x", "params": [], "body_encoding": "none"}
    import pytest

    with pytest.raises(rb.RequestBuildError, match="newlines"):
        rb.build_request(
            "https://api.example.com",
            tool,
            {},
            credentials=[
                {"location": "header", "name": "X", "format": "{token}", "value": "a\r\nB: c"}
            ],
        )


def test_an_unconfigured_credential_is_simply_not_sent():
    tool = {"name": "t", "method": "GET", "path": "/x", "params": [], "body_encoding": "none"}
    request = rb.build_request(
        "https://api.example.com",
        tool,
        {},
        credentials=[{"location": "header", "name": "X", "format": "{token}", "value": None}],
    )
    assert "X" not in request["headers"]
