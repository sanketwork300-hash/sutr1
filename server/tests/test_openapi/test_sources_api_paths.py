"""Registry API paths built from a pasted URL stay on the endpoint they name."""

import pytest

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.sources import (
    fetch_from_github,
    fetch_from_swaggerhub,
    fetch_github_file,
    parse_github_url,
)


async def test_dot_segments_in_a_github_url_are_refused(monkeypatch):
    async def never(path, token):  # pragma: no cover - must not be reached
        raise AssertionError(f"GitHub was called with {path}")

    monkeypatch.setattr("sutr.openapi.sources._github_json", never)
    with pytest.raises(OpenAPIError) as exc:
        await fetch_from_github("https://github.com/../user/blob/main/openapi.yaml")
    assert exc.value.code == "bad_source_url"


async def test_a_file_path_cannot_walk_out_of_the_contents_endpoint(monkeypatch):
    async def never(path, token):  # pragma: no cover - must not be reached
        raise AssertionError(f"GitHub was called with {path}")

    monkeypatch.setattr("sutr.openapi.sources._github_json", never)
    target = parse_github_url("https://github.com/acme/api")
    with pytest.raises(OpenAPIError):
        await fetch_github_file(target, "../../../../user", "main", token="ghp_secret")


async def test_path_characters_are_encoded_within_their_segment(monkeypatch):
    seen = []

    async def fake(path, token):
        seen.append(path)
        return {"encoding": "base64", "content": "e30=", "size": 2}

    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)
    target = parse_github_url("https://github.com/acme/api")
    await fetch_github_file(target, "specs/a?b#c.yaml", "main")
    assert seen == ["/repos/acme/api/contents/specs/a%3Fb%23c.yaml?ref=main"]


async def test_the_token_is_not_sent_to_a_download_url_off_github(monkeypatch):
    async def fake(path, token):
        return {"size": 10, "download_url": "https://evil.example.com/spec.yaml"}

    async def never(url, headers=None):  # pragma: no cover - must not be reached
        raise AssertionError(f"fetched {url}")

    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)
    monkeypatch.setattr("sutr.openapi.loader.fetch_spec_from_url", never)
    target = parse_github_url("https://github.com/acme/api")
    with pytest.raises(OpenAPIError):
        await fetch_github_file(target, "openapi.yaml", "main", token="ghp_secret")


async def test_swaggerhub_segments_are_checked_too(monkeypatch):
    async def never(url, headers=None):  # pragma: no cover - must not be reached
        raise AssertionError(f"fetched {url}")

    monkeypatch.setattr("sutr.openapi.loader.fetch_spec_from_url", never)
    with pytest.raises(OpenAPIError):
        await fetch_from_swaggerhub("https://app.swaggerhub.com/apis/acme/../..", api_key="k")
