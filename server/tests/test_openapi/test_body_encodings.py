"""Request bodies of every supported encoding (build prompt §26, ADR-008).

Before this, anything that was not `application/json` produced a warning and a
tool that posted nothing. Each encoding below is followed all the way through:
spec → IR → compiled tool → the request the runtime actually builds.

The last test is the ADR-010 guarantee: the hosted gateway and the generated
standalone package produce the *same* request, checked by running both.
"""

import base64
import importlib.util
import io
import json
import zipfile

import pytest

from sutr.openapi.compiler import compile_definition
from sutr.openapi.normalizer import normalize
from sutr.openapi.packaging import build_server_package
from sutr.openapi.security import translate_security
from sutr.runtime import request_builder as rb


def _spec(request_body: dict, *, method: str = "post") -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Bodies", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/things": {
                method: {
                    "operationId": "submit",
                    "summary": "Submit a thing.",
                    "requestBody": request_body,
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _compile(request_body: dict):
    definition = normalize(_spec(request_body))
    result = compile_definition(definition)
    return definition, result.tools[0].tool, result.warnings


# ── JSON stays exactly as it was ─────────────────────────────────────────────


def test_json_body_is_unchanged():
    _, tool, _ = _compile(
        {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}, "size": {"type": "integer"}},
                    }
                }
            },
        }
    )
    assert tool.body_encoding == "json"
    assert tool.body_content_type == "application/json"
    assert {p.name for p in tool.params} == {"name", "size"}
    request = rb.build_request("https://api.example.com", tool.model_dump(), {"name": "Rex"})
    assert request["body"] == {"kind": "json", "json": {"name": "Rex"}}


def test_a_json_subtype_is_preserved_not_normalized():
    _, tool, _ = _compile(
        {
            "content": {
                "application/merge-patch+json": {
                    "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                }
            }
        }
    )
    assert tool.body_encoding == "json"
    assert tool.body_content_type == "application/merge-patch+json"


# ── Form ─────────────────────────────────────────────────────────────────────


def test_form_urlencoded_body_becomes_form_fields():
    definition, tool, _ = _compile(
        {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "required": ["grant_type"],
                        "properties": {
                            "grant_type": {"type": "string"},
                            "scope": {"type": "string"},
                        },
                    }
                }
            },
        }
    )
    assert definition.operations[0].request_body.encoding == "form"
    assert tool.body_encoding == "form"
    assert {p.name for p in tool.params} == {"grant_type", "scope"}
    assert {p.name for p in tool.params if p.required} == {"grant_type"}
    request = rb.build_request(
        "https://api.example.com", tool.model_dump(), {"grant_type": "password", "scope": "read"}
    )
    assert request["body"]["kind"] == "form"
    assert request["body"]["data"] == {"grant_type": "password", "scope": "read"}


# ── Multipart ────────────────────────────────────────────────────────────────


def test_multipart_body_with_a_file_part():
    _, tool, _ = _compile(
        {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "caption": {"type": "string"},
                            "file": {"type": "string", "format": "binary"},
                        },
                    }
                }
            }
        }
    )
    assert tool.body_encoding == "multipart"
    file_param = next(p for p in tool.params if p.name == "file")
    assert file_param.schema_override["format"] == "binary"
    assert "base64" in file_param.description

    request = rb.build_request(
        "https://api.example.com",
        tool.model_dump(),
        {"caption": "hi", "file": base64.b64encode(b"PNGDATA").decode()},
    )
    assert request["body"]["data"] == {"caption": "hi"}
    assert request["body"]["files"]["file"][1] == b"PNGDATA"


# ── Binary ───────────────────────────────────────────────────────────────────


def test_octet_stream_body_becomes_one_base64_parameter():
    _, tool, _ = _compile(
        {
            "required": True,
            "description": "The raw file.",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    )
    assert tool.body_encoding == "binary"
    assert tool.body_content_type == "application/octet-stream"
    assert len(tool.params) == 1
    param = tool.params[0]
    assert param.required is True
    assert "base64" in param.description

    request = rb.build_request(
        "https://api.example.com",
        tool.model_dump(),
        {param.name: base64.b64encode(b"\x89PNG").decode()},
    )
    assert request["body"] == {"kind": "binary", "content": b"\x89PNG"}
    assert request["headers"]["Content-Type"] == "application/octet-stream"


def test_an_image_media_type_is_binary_too():
    _, tool, _ = _compile({"content": {"image/png": {"schema": {"type": "string"}}}})
    assert tool.body_encoding == "binary"
    assert tool.body_content_type == "image/png"


# ── Text ─────────────────────────────────────────────────────────────────────


def test_text_body_becomes_one_string_parameter():
    _, tool, _ = _compile({"content": {"text/csv": {"schema": {"type": "string"}}}})
    assert tool.body_encoding == "text"
    assert tool.body_content_type == "text/csv"
    request = rb.build_request(
        "https://api.example.com", tool.model_dump(), {tool.params[0].name: "a,b\n1,2"}
    )
    assert request["body"] == {"kind": "text", "content": "a,b\n1,2"}
    assert request["headers"]["Content-Type"] == "text/csv"


def test_xml_body_is_text():
    _, tool, _ = _compile({"content": {"application/xml": {"schema": {"type": "string"}}}})
    assert tool.body_encoding == "text"


# ── Selection and refusal ────────────────────────────────────────────────────


def test_json_is_preferred_when_several_media_types_are_offered():
    definition, tool, _ = _compile(
        {
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {"type": "object", "properties": {"a": {"type": "string"}}}
                },
                "application/json": {
                    "schema": {"type": "object", "properties": {"a": {"type": "string"}}}
                },
            }
        }
    )
    assert tool.body_encoding == "json"
    assert any(w.code == "body_media_type_selected" for w in definition.warnings)


def test_an_unconstructible_media_type_warns_and_sends_no_body():
    definition, tool, _ = _compile(
        {"content": {"application/vnd.company.proprietary-frame": {"schema": {}}}}
    )
    assert tool.body_encoding == "none"
    assert any(w.code == "unsupported_body" for w in definition.warnings)
    assert rb.build_body(tool.model_dump(), {"anything": 1})["kind"] == "none"


def test_a_form_body_with_no_properties_warns_rather_than_guessing():
    definition = normalize(
        _spec({"content": {"application/x-www-form-urlencoded": {"schema": {"type": "object"}}}})
    )
    result = compile_definition(definition)
    assert any(w.code == "unsupported_body" for w in result.warnings)
    assert result.tools[0].tool.body_encoding == "form"
    assert result.tools[0].tool.params == []


def test_an_operation_with_no_request_body_declares_no_encoding():
    definition = normalize(
        {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/things": {
                    "get": {
                        "operationId": "listThings",
                        "summary": "List.",
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    tool = compile_definition(definition).tools[0].tool
    assert tool.body_encoding == "none"


# ── ADR-010: both runtimes agree ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_body,args",
    [
        (
            {
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                    }
                }
            },
            {"name": "Rex"},
        ),
        (
            {
                "content": {
                    "application/x-www-form-urlencoded": {
                        "schema": {
                            "type": "object",
                            "properties": {"grant_type": {"type": "string"}},
                        }
                    }
                }
            },
            {"grant_type": "client_credentials"},
        ),
        (
            {
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "caption": {"type": "string"},
                                "file": {"type": "string", "format": "binary"},
                            },
                        }
                    }
                }
            },
            {"caption": "hi", "file": base64.b64encode(b"BYTES").decode()},
        ),
        (
            {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            },
            {"body": base64.b64encode(b"RAW").decode()},
        ),
        (
            {"content": {"text/plain": {"schema": {"type": "string"}}}},
            {"body": "plain text"},
        ),
    ],
    ids=["json", "form", "multipart", "binary", "text"],
)
def test_hosted_and_standalone_runtimes_build_the_same_request(request_body, args, tmp_path):
    definition = normalize(_spec(request_body))
    result = compile_definition(definition)
    tool = result.tools[0].tool
    auth = translate_security(definition)

    _, package = build_server_package(
        name="Bodies",
        base_url="https://api.example.com",
        token_header=auth.token_header,
        token_format=auth.token_format,
        tools=[tool],
        api_title=definition.title,
        api_version=definition.version,
    )
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        archive.extractall(tmp_path)

    spec = importlib.util.spec_from_file_location("gen_rt", tmp_path / "sutr_runtime.py")
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    bundle = json.loads((tmp_path / "tools.json").read_text())

    # The wrapped-body parameter is named by the compiler; use whatever it chose.
    if tool.body_param and tool.body_param not in args:
        args = {tool.body_param: next(iter(args.values()))}

    hosted = rb.build_request("https://api.example.com", tool.model_dump(), args)
    standalone = generated.build_tool_request(bundle, bundle["tools"][0], args)
    assert hosted == standalone
