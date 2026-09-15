"""Source connectors: seven verbs, one normalization pipeline (LLD §3.3).

The property worth protecting is the one the LLD states directly — *"the
compiler never knows, or cares, where a spec came from"*. So these tests check
the interface holds uniformly, and that each connector's own behaviour (a
conditional fetch, a discovery, a conversion) works.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from sutr.source_connectors import registry
from sutr.source_connectors.base import (
    WATCH_NONE,
    WATCH_POLL,
    Provenance,
)
from sutr.source_connectors.inline import PasteConnector, UploadConnector
from sutr.source_connectors.postman import PostmanConnector, convert_collection
from sutr.source_connectors.url import SwaggerUiConnector, UrlConnector

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}
SPEC_TEXT = json.dumps(SPEC)


# ── The interface holds for every connector ──────────────────────────────────


def test_every_connector_implements_all_seven_verbs():
    """Build prompt §12 fixes the seven. Three have one correct implementation
    and live on the base class, which is why every connector really has them
    rather than each reinventing three."""
    for connector in registry.all_connectors():
        for verb in (
            "connect",
            "discover",
            "fetch",
            "validate",
            "watch_plan",
            "detect_drift",
            "disconnect",
        ):
            assert callable(getattr(connector, verb, None)), f"{connector.id} lacks {verb}"


def test_every_connector_describes_itself():
    for connector in registry.all_connectors():
        described = connector.describe()
        assert described["id"] and described["display_name"] and described["description"]
        assert described["available"] is True


def test_a_connector_that_cannot_be_watched_says_why():
    for connector in registry.all_connectors():
        if connector.supports_watch:
            continue
        plan = connector.watch_plan({})
        assert plan.mode == WATCH_NONE
        assert plan.reason, f"{connector.id} refuses to be watched without saying why"


def test_watched_connectors_declare_whether_polling_is_cheap():
    """A poll that transfers the whole document every time is a different
    proposition from one that gets a 304, and the plan says which."""
    for connector in registry.all_connectors():
        if not connector.supports_watch:
            continue
        plan = connector.watch_plan({"url": "https://api.example.com/openapi.json"})
        assert plan.mode == WATCH_POLL
        assert isinstance(plan.conditional, bool)
        assert plan.reason


def test_validation_is_shared_rather_than_per_connector():
    """A document's validity has nothing to do with where it was found."""
    results = [connector.validate(SPEC_TEXT) for connector in registry.all_connectors()]
    assert all(result.valid for result in results)
    assert {result.api_title for result in results} == {"Pets"}


def test_validation_reports_rather_than_raises():
    result = PasteConnector().validate("{not a spec")
    assert result.valid is False
    assert result.error_code
    assert result.message


def test_drift_detection_is_shared_too():
    from sutr.openapi.normalizer import normalize

    before = normalize(json.loads(SPEC_TEXT))
    after_doc = json.loads(SPEC_TEXT)
    del after_doc["paths"]["/pets"]
    after_doc["paths"]["/other"] = SPEC["paths"]["/pets"]
    report = PasteConnector().detect_drift(before, normalize(after_doc))
    assert report.breaking


def test_planned_connectors_are_listed_with_a_reason():
    """A source type silently missing looks like an oversight; one listed as
    unavailable with a reason is a decision."""
    described = registry.describe_all()
    planned = {entry["id"]: entry for entry in described["planned"]}
    assert {"wsdl", "api_gateway", "generic_git"} <= set(planned)
    for entry in planned.values():
        assert entry["available"] is False
        assert len(entry["unavailable_reason"]) > 40


def test_requiring_an_unknown_connector_names_the_available_ones():
    from sutr.source_connectors.base import ConnectorError

    with pytest.raises(ConnectorError) as excinfo:
        registry.require("carrier_pigeon")
    assert "github" in excinfo.value.message


# ── Inline connectors ────────────────────────────────────────────────────────


async def test_paste_returns_what_it_was_given():
    result = await PasteConnector().fetch({"content": SPEC_TEXT}, {})
    assert result.content == SPEC_TEXT
    assert result.provenance.source_type == "paste"
    assert result.not_modified is False


async def test_paste_refuses_when_empty():
    connection = await PasteConnector().connect({"content": "  "}, {})
    assert connection.connected is False


async def test_upload_keeps_the_filename_as_provenance():
    result = await UploadConnector().fetch({"content": SPEC_TEXT, "filename": "petstore.yaml"}, {})
    assert result.provenance.detail["filename"] == "petstore.yaml"


# ── URL connector: conditional fetching ──────────────────────────────────────


def _response(status: int, *, body: str = "", headers: dict | None = None):
    return httpx.Response(
        status,
        content=body.encode(),
        headers=headers or {},
        request=httpx.Request("GET", "https://api.example.com/openapi.json"),
    )


class _Stream:
    """Minimal stand-in for httpx's streaming context manager."""

    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


def _patch_stream(response, captured: dict):
    def stream(self, method, url, **kwargs):
        captured["headers"] = kwargs.get("headers") or {}
        captured["url"] = url

        async def _iter():
            yield response.content

        response.aiter_bytes = lambda: _iter()
        return _Stream(response)

    return patch("httpx.AsyncClient.stream", stream)


async def test_a_url_fetch_records_etag_and_last_modified(monkeypatch):
    monkeypatch.setattr("sutr.source_connectors.url.validate_safe_url", lambda url: None)
    captured: dict = {}
    response = _response(
        200,
        body=SPEC_TEXT,
        headers={"etag": '"v1"', "last-modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    with _patch_stream(response, captured):
        result = await UrlConnector().fetch({"url": "https://api.example.com/openapi.json"}, {})
    assert result.content == SPEC_TEXT
    assert result.provenance.etag == '"v1"'
    assert result.provenance.last_modified.startswith("Wed")
    assert result.provenance.source_version == '"v1"'


async def test_a_url_fetch_sends_what_it_already_has(monkeypatch):
    """This is what makes polling cheap rather than rude."""
    monkeypatch.setattr("sutr.source_connectors.url.validate_safe_url", lambda url: None)
    captured: dict = {}
    with _patch_stream(_response(304), captured):
        result = await UrlConnector().fetch(
            {"url": "https://api.example.com/openapi.json"},
            {},
            known=Provenance(
                source_type="url",
                source_uri="https://api.example.com/openapi.json",
                etag='"v1"',
                last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
            ),
        )
    assert captured["headers"]["If-None-Match"] == '"v1"'
    assert captured["headers"]["If-Modified-Since"].startswith("Wed")
    assert result.not_modified is True
    assert result.content is None


async def test_a_304_carries_the_known_provenance_forward(monkeypatch):
    monkeypatch.setattr("sutr.source_connectors.url.validate_safe_url", lambda url: None)
    known = Provenance(source_type="url", source_uri="https://x/openapi.json", etag='"keep"')
    with _patch_stream(_response(304), {}):
        result = await UrlConnector().fetch({"url": "https://x/openapi.json"}, {}, known=known)
    assert result.provenance.etag == '"keep"'
    assert result.provenance.retrieved_at


async def test_a_redirect_is_refused_rather_than_followed(monkeypatch):
    """Following one could bounce a credential to another host."""
    monkeypatch.setattr("sutr.source_connectors.url.validate_safe_url", lambda url: None)
    from sutr.source_connectors.base import ConnectorError

    with _patch_stream(_response(302), {}):
        with pytest.raises(ConnectorError, match="redirect"):
            await UrlConnector().fetch({"url": "https://x/openapi.json"}, {})


async def test_an_unsafe_url_is_refused_before_any_request():
    from sutr.source_connectors.base import ConnectorError

    with pytest.raises(ConnectorError, match="[Uu]nsafe"):
        await UrlConnector().fetch({"url": "http://169.254.169.254/openapi.json"}, {})


# ── Swagger UI discovery ─────────────────────────────────────────────────────


async def test_swagger_ui_finds_the_spec_behind_the_page():
    """The LLD calls this out: many teams only know the UI URL."""
    page = """
    <html><body><script>
      window.ui = SwaggerUIBundle({ url: "/v3/api-docs", dom_id: '#swagger-ui' });
    </script></body></html>
    """
    fetched = {}

    async def fake_fetch(url, headers=None):
        fetched.setdefault("urls", []).append(url)
        if url.endswith("index.html"):
            return page
        if url.endswith("/v3/api-docs"):
            return SPEC_TEXT
        raise __import__("sutr.openapi.errors", fromlist=["OpenAPIError"]).OpenAPIError(
            "fetch_failed", "nope"
        )

    with patch("sutr.source_connectors.url.fetch_spec_from_url", fake_fetch):
        found = await SwaggerUiConnector().discover(
            {"url": "https://api.example.com/swagger-ui/index.html"}, {}
        )
    assert found
    assert found[0].identifier == "https://api.example.com/v3/api-docs"


async def test_swagger_ui_ignores_its_own_assets():
    """Guessing by filename would offer swagger-ui.css as an API definition."""
    page = """<script>SwaggerUIBundle({ url: "./swagger-ui.css" })</script>"""

    async def fake_fetch(url, headers=None):
        if url.endswith("index.html"):
            return page
        from sutr.openapi.errors import OpenAPIError

        raise OpenAPIError("fetch_failed", "nope")

    with patch("sutr.source_connectors.url.fetch_spec_from_url", fake_fetch):
        found = await SwaggerUiConnector().discover(
            {"url": "https://api.example.com/swagger-ui/index.html"}, {}
        )
    assert found == []


async def test_swagger_ui_falls_back_to_conventional_paths():
    async def fake_fetch(url, headers=None):
        if url.endswith("/swagger.json"):
            return SPEC_TEXT
        from sutr.openapi.errors import OpenAPIError

        raise OpenAPIError("fetch_failed", "nope")

    with patch("sutr.source_connectors.url.fetch_spec_from_url", fake_fetch):
        found = await SwaggerUiConnector().discover(
            {"url": "https://api.example.com/swagger-ui/index.html"}, {}
        )
    assert found
    assert found[0].identifier.endswith("/swagger.json")


async def test_swagger_ui_says_so_when_the_spec_is_inline():
    from sutr.source_connectors.base import ConnectorError

    page = """<script>SwaggerUIBundle({ spec: { openapi: "3.0.0" } })</script>"""

    async def fake_fetch(url, headers=None):
        if url.endswith("index.html"):
            return page
        from sutr.openapi.errors import OpenAPIError

        raise OpenAPIError("fetch_failed", "nope")

    with patch("sutr.source_connectors.url.fetch_spec_from_url", fake_fetch):
        with pytest.raises(ConnectorError, match="inline"):
            await SwaggerUiConnector().discover(
                {"url": "https://api.example.com/swagger-ui/index.html"}, {}
            )


# ── Postman ──────────────────────────────────────────────────────────────────


COLLECTION = {
    "info": {
        "name": "Pets",
        "_postman_id": "abc",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "auth": {
        "type": "apikey",
        "apikey": [{"key": "key", "value": "X-Api-Key"}, {"key": "in", "value": "header"}],
    },
    "variable": [{"key": "base", "value": "https://api.example.com"}],
    "item": [
        {
            "name": "Pets",
            "item": [
                {
                    "name": "List pets",
                    "request": {
                        "method": "GET",
                        "url": {
                            "raw": "{{base}}/pets?limit=10",
                            "query": [{"key": "limit", "value": "10"}],
                        },
                    },
                },
                {
                    "name": "Get pet",
                    "request": {"method": "GET", "url": {"raw": "{{base}}/pets/:petId"}},
                },
            ],
        }
    ],
}


def test_a_collection_becomes_a_valid_specification():
    document, notes = convert_collection(COLLECTION)
    assert document["openapi"].startswith("3.0")
    assert document["servers"] == [{"url": "https://api.example.com"}]
    assert set(document["paths"]) == {"/pets", "/pets/{petId}"}
    assert notes


def test_the_conversion_says_it_inferred_types():
    """A collection records example requests, not a contract. Saying so is the
    difference between a useful tool and a confidently wrong one."""
    _, notes = convert_collection(COLLECTION)
    assert any("inferred" in note for note in notes)


def test_collection_variables_are_substituted():
    document, _ = convert_collection(COLLECTION)
    assert "{{base}}" not in json.dumps(document)


def test_postman_path_parameters_become_openapi_ones():
    document, _ = convert_collection(COLLECTION)
    operation = document["paths"]["/pets/{petId}"]["get"]
    assert {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}} in (
        operation["parameters"]
    )


def test_query_types_are_inferred_from_the_example_value():
    document, _ = convert_collection(COLLECTION)
    limit = next(p for p in document["paths"]["/pets"]["get"]["parameters"] if p["name"] == "limit")
    assert limit["schema"]["type"] == "integer"


def test_collection_auth_becomes_a_security_scheme():
    document, _ = convert_collection(COLLECTION)
    assert document["components"]["securitySchemes"]["apiKeyAuth"] == {
        "type": "apiKey",
        "name": "X-Api-Key",
        "in": "header",
    }


def test_an_authorization_header_does_not_become_a_tool_argument():
    """Emitting it as a parameter would put a credential in the argument list."""
    collection = json.loads(json.dumps(COLLECTION))
    collection["item"][0]["item"][0]["request"]["header"] = [
        {"key": "Authorization", "value": "Bearer x"},
        {"key": "X-Trace", "value": "1"},
    ]
    document, _ = convert_collection(collection)
    names = {p["name"] for p in document["paths"]["/pets"]["get"].get("parameters", [])}
    assert "Authorization" not in names
    assert "X-Trace" in names


async def test_a_v1_collection_is_refused_with_the_fix():
    from sutr.source_connectors.base import ConnectorError

    old = {
        "info": {"name": "Old", "_postman_id": "x", "schema": "…/collection/v1.0.0/…"},
        "item": [],
    }
    with pytest.raises(ConnectorError, match="v2.1"):
        await PostmanConnector().fetch({"content": json.dumps(old)}, {})


async def test_something_that_is_not_a_collection_is_refused():
    from sutr.source_connectors.base import ConnectorError

    with pytest.raises(ConnectorError, match="Postman collection"):
        await PostmanConnector().fetch({"content": SPEC_TEXT}, {})


async def test_the_converted_collection_translates_end_to_end():
    result = await PostmanConnector().fetch({"content": json.dumps(COLLECTION)}, {})
    validation = PostmanConnector().validate(result.content)
    assert validation.valid
    assert validation.operation_count == 2
