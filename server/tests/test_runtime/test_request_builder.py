"""The shared request builder — one definition of how a tool becomes a request.

ADR-008 (every body encoding is compiled, never silently dropped) and ADR-010
(the hosted gateway and a generated standalone server build identical requests)
both land here, because this module is what makes both true.
"""

import base64

import pytest

from sutr.runtime import RuntimeMode
from sutr.runtime import request_builder as rb


def _tool(**overrides) -> dict:
    tool = {
        "name": "t",
        "description": "",
        "method": "POST",
        "path": "/things/{id}",
        "params": [],
        "body_param": None,
        "body_encoding": "json",
        "body_content_type": None,
    }
    tool.update(overrides)
    return tool


def _param(name, **overrides) -> dict:
    param = {
        "name": name,
        "type": "string",
        "description": None,
        "required": False,
        "default": None,
        "enum": None,
        "items": None,
        "query": False,
        "schema_override": None,
        "location": None,
        "wire_name": None,
    }
    param.update(overrides)
    return param


# ── Placement ────────────────────────────────────────────────────────────────


def test_path_params_are_percent_encoded():
    url = rb.build_url("https://api.example.com/", "/things/{id}", {"id": "a/b c"})
    assert url == "https://api.example.com/things/a%2Fb%20c"


def test_query_uses_the_wire_name_and_drops_none():
    params = [
        _param("q", location="query"),
        _param("api_version", location="query", wire_name="api-version"),
        _param("skipped", location="query"),
    ]
    query = rb.build_query(params, {"q": "x", "api_version": "2", "skipped": None})
    assert query == {"q": "x", "api-version": "2"}


def test_auth_headers_always_win_over_argument_headers():
    tool = _tool(params=[_param("h", location="header", wire_name="X-Api-Key")])
    request = rb.build_request(
        "https://api.example.com", tool, {"h": "attacker"}, {"X-Api-Key": "real"}
    )
    assert request["headers"]["X-Api-Key"] == "real"


# ── JSON bodies ──────────────────────────────────────────────────────────────


def test_json_body_collects_unconsumed_arguments():
    tool = _tool(
        params=[
            _param("id", location="path"),
            _param("q", location="query"),
            _param("name", location="body"),
            _param("size", location="body"),
        ]
    )
    request = rb.build_request(
        "https://x.example.com", tool, {"id": "1", "q": "z", "name": "Rex", "size": 3}
    )
    assert request["body"] == {"kind": "json", "json": {"name": "Rex", "size": 3}}


def test_json_body_param_sends_the_raw_value():
    tool = _tool(body_param="body", params=[_param("body", location="body")])
    request = rb.build_request("https://x.example.com", tool, {"body": [1, 2, 3]})
    assert request["body"] == {"kind": "json", "json": [1, 2, 3]}


def test_a_non_default_json_media_type_is_preserved():
    tool = _tool(
        body_content_type="application/merge-patch+json",
        params=[_param("name", location="body")],
    )
    request = rb.build_request("https://x.example.com", tool, {"name": "Rex"})
    assert request["headers"]["Content-Type"] == "application/merge-patch+json"


def test_no_body_when_nothing_maps_to_it():
    tool = _tool(params=[_param("id", location="path")])
    assert rb.build_request("https://x.example.com", tool, {"id": "1"})["body"]["kind"] == "none"


# ── Form bodies ──────────────────────────────────────────────────────────────


def test_form_body_produces_string_fields():
    tool = _tool(
        body_encoding="form",
        body_content_type="application/x-www-form-urlencoded",
        params=[_param("grant_type", location="body"), _param("expires", location="body")],
    )
    request = rb.build_request(
        "https://x.example.com", tool, {"grant_type": "client_credentials", "expires": 3600}
    )
    assert request["body"] == {
        "kind": "form",
        "data": {"grant_type": "client_credentials", "expires": "3600"},
    }
    assert rb.request_kwargs(request)["data"] == request["body"]["data"]
    assert "json" not in rb.request_kwargs(request)


def test_form_booleans_are_lowercase_and_structures_are_json():
    tool = _tool(
        body_encoding="form",
        params=[_param("flag", location="body"), _param("meta", location="body")],
    )
    body = rb.build_request("https://x.example.com", tool, {"flag": True, "meta": {"a": 1}})["body"]
    assert body["data"]["flag"] == "true"
    assert body["data"]["meta"] == '{"a": 1}'


def test_form_uses_the_wire_name():
    tool = _tool(
        body_encoding="form",
        params=[_param("client_id", location="body", wire_name="client-id")],
    )
    body = rb.build_request("https://x.example.com", tool, {"client_id": "abc"})["body"]
    assert body["data"] == {"client-id": "abc"}


# ── Multipart bodies ─────────────────────────────────────────────────────────


def test_multipart_splits_binary_parts_from_text_fields():
    tool = _tool(
        body_encoding="multipart",
        body_content_type="multipart/form-data",
        params=[
            _param("caption", location="body"),
            _param(
                "file",
                location="body",
                schema_override={"type": "string", "format": "binary"},
            ),
        ],
    )
    payload = base64.b64encode(b"hello bytes").decode()
    request = rb.build_request(
        "https://x.example.com", tool, {"caption": "a photo", "file": payload}
    )
    body = request["body"]
    assert body["kind"] == "multipart"
    assert body["data"] == {"caption": "a photo"}
    assert body["files"]["file"][1] == b"hello bytes"


def test_multipart_lets_httpx_set_the_boundary():
    """A caller-supplied multipart Content-Type would have no boundary."""
    tool = _tool(
        body_encoding="multipart",
        params=[
            _param("f", location="body", schema_override={"type": "string", "format": "binary"})
        ],
    )
    request = rb.build_request(
        "https://x.example.com",
        tool,
        {"f": base64.b64encode(b"x").decode()},
        {"Content-Type": "multipart/form-data"},
    )
    kwargs = rb.request_kwargs(request)
    assert "content-type" not in {k.lower() for k in kwargs["headers"]}
    assert kwargs["files"]


# ── Binary and text bodies ───────────────────────────────────────────────────


def test_binary_body_base64_decodes_the_argument():
    tool = _tool(
        body_encoding="binary",
        body_content_type="application/octet-stream",
        body_param="body",
        params=[_param("body", location="body")],
    )
    request = rb.build_request(
        "https://x.example.com", tool, {"body": base64.b64encode(b"\x00\x01raw").decode()}
    )
    assert request["body"] == {"kind": "binary", "content": b"\x00\x01raw"}
    assert request["headers"]["Content-Type"] == "application/octet-stream"
    assert rb.request_kwargs(request)["content"] == b"\x00\x01raw"


def test_invalid_base64_is_a_clear_error_not_a_corrupt_request():
    tool = _tool(
        body_encoding="binary", body_param="body", params=[_param("body", location="body")]
    )
    with pytest.raises(rb.RequestBuildError, match="not valid base64"):
        rb.build_request("https://x.example.com", tool, {"body": "!!!not base64!!!"})


def test_text_body_is_sent_verbatim_with_the_declared_content_type():
    tool = _tool(
        body_encoding="text",
        body_content_type="application/xml",
        body_param="body",
        params=[_param("body", location="body")],
    )
    request = rb.build_request("https://x.example.com", tool, {"body": "<a/>"})
    assert request["body"] == {"kind": "text", "content": "<a/>"}
    assert request["headers"]["Content-Type"] == "application/xml"


def test_a_get_never_carries_a_body_whatever_the_encoding():
    for encoding in ("json", "form", "multipart", "binary", "text"):
        tool = _tool(method="GET", body_encoding=encoding, params=[_param("x", location="body")])
        assert rb.build_body(tool, {"x": "value"})["kind"] == "none"


def test_delete_may_carry_a_body_when_the_spec_declared_one():
    tool = _tool(method="DELETE", params=[_param("reason", location="body")])
    assert rb.build_body(tool, {"reason": "cleanup"}) == {
        "kind": "json",
        "json": {"reason": "cleanup"},
    }


# ── Schema ───────────────────────────────────────────────────────────────────


def test_input_schema_lists_required_properties():
    schema = rb.input_schema(
        [_param("a", required=True, description="First."), _param("b", enum=["x", "y"])]
    )
    assert schema["required"] == ["a"]
    assert schema["properties"]["a"]["description"] == "First."
    assert schema["properties"]["b"]["enum"] == ["x", "y"]


def test_schema_override_replaces_everything_else():
    override = {"type": "object", "properties": {"nested": {"type": "string"}}}
    schema = rb.input_schema([_param("payload", schema_override=override)])
    assert schema["properties"]["payload"] == override


def test_runtime_modes_are_named():
    assert RuntimeMode.HOSTED.value == "HOSTED"
    assert RuntimeMode.STANDALONE.value == "STANDALONE"
