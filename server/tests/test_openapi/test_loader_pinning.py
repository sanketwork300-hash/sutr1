"""SECURITY_BUGS.md S11: a specification fetch connects to the address the
SSRF screen vetted, not to whatever the name answers a second time."""

import httpx

from sutr.openapi.loader import fetch_spec_from_url

PUBLIC = "93.184.216.34"


async def test_fetch_spec_connects_to_the_vetted_address(monkeypatch):
    monkeypatch.setattr("sutr.upstream_safety._resolve_host", lambda hostname, port: (PUBLIC,))
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, content=b'{"openapi": "3.0.0"}')

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.openapi.loader.httpx.AsyncClient", Patched)

    text = await fetch_spec_from_url(
        "https://specs.example.com/openapi.json", headers={"Authorization": "Bearer t"}
    )

    assert text == '{"openapi": "3.0.0"}'
    [request] = sent
    assert request.url.host == PUBLIC
    assert request.headers["host"] == "specs.example.com"
    assert request.headers["authorization"] == "Bearer t"
    assert request.extensions["sni_hostname"] == "specs.example.com"
