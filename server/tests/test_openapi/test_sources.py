"""Import sources: GitHub and SwaggerHub URL parsing, discovery, and fetching.

The security property under test is that a source credential only ever travels
to that source's host.
"""

import base64
import json

import pytest

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.sources import (
    SPEC_FILENAMES,
    detect_source_kind,
    discover_github_specs,
    fetch_from_github,
    fetch_from_swaggerhub,
    parse_github_url,
    parse_swaggerhub_url,
)

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "responses": {"200": {"description": "OK"}}}}
    },
}


# ── GitHub URL parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/api", ("acme", "api", None, None)),
        ("https://github.com/acme/api.git", ("acme", "api", None, None)),
        ("https://github.com/acme/api/tree/develop", ("acme", "api", "develop", None)),
        ("https://github.com/acme/api/tree/main/specs", ("acme", "api", "main", None)),
        (
            "https://github.com/acme/api/blob/main/specs/openapi.yaml",
            ("acme", "api", "main", "specs/openapi.yaml"),
        ),
        (
            "https://raw.githubusercontent.com/acme/api/main/openapi.json",
            ("acme", "api", "main", "openapi.json"),
        ),
    ],
)
def test_parse_github_url_shapes(url, expected):
    target = parse_github_url(url)
    assert (target.owner, target.repo, target.branch, target.path) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/api",
        "https://github.com/acme",
        "ftp://github.com/acme/api",
        "not a url",
    ],
)
def test_parse_github_url_rejects_bad_input(url):
    with pytest.raises(OpenAPIError) as exc:
        parse_github_url(url)
    assert exc.value.code == "bad_source_url"


def test_tree_url_is_a_search_root_not_a_file():
    """A /tree/ URL names a directory: never fetched as a file, but still used
    to scope discovery."""
    target = parse_github_url("https://github.com/acme/api/tree/main/specs")
    assert target.path is None
    assert target.directory == "specs"


# ── GitHub discovery ─────────────────────────────────────────────────────────


class FakeGitHub:
    """Records requests and replays canned GitHub API responses."""

    def __init__(self, tree: list[dict], default_branch: str = "main"):
        self.tree = tree
        self.default_branch = default_branch
        self.requests: list[tuple[str, dict]] = []

    async def __call__(self, path: str, token: str | None):
        self.requests.append((path, {"token": token}))
        if "/git/trees/" in path:
            return {"tree": self.tree, "truncated": False}
        if "/contents/" in path:
            file_path = path.split("/contents/")[1].split("?")[0]
            return {
                "path": file_path,
                "size": 120,
                "encoding": "base64",
                "content": base64.b64encode(json.dumps(SPEC).encode()).decode(),
            }
        return {"default_branch": self.default_branch}


def _blob(path: str, size: int = 100) -> dict:
    return {"type": "blob", "path": path, "size": size}


async def test_discovery_prefers_conventional_names(monkeypatch):
    fake = FakeGitHub(
        [
            _blob("docs/notes.md"),
            _blob("deep/nested/dir/openapi.json"),
            _blob("swagger.yaml"),
            _blob("openapi.yaml"),
            _blob("other/petstore.openapi.yaml"),
        ]
    )
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    candidates, branch = await discover_github_specs(parse_github_url("https://github.com/a/b"))

    assert branch == "main"
    paths = [candidate.path for candidate in candidates]
    # The rule: openapi.* family first, then shallower, then YAML before JSON,
    # with merely spec-shaped names last.
    assert paths == [
        "openapi.yaml",
        "deep/nested/dir/openapi.json",
        "swagger.yaml",
        "other/petstore.openapi.yaml",
    ]
    assert "docs/notes.md" not in paths


async def test_discovery_scopes_to_a_tree_path(monkeypatch):
    fake = FakeGitHub([_blob("openapi.yaml"), _blob("specs/openapi.yaml")])
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    candidates, _branch = await discover_github_specs(
        parse_github_url("https://github.com/a/b/tree/main/specs")
    )
    assert [c.path for c in candidates] == ["specs/openapi.yaml"]


async def test_discovery_uses_the_repo_default_branch(monkeypatch):
    fake = FakeGitHub([_blob("openapi.yaml")], default_branch="trunk")
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    _candidates, branch = await discover_github_specs(parse_github_url("https://github.com/a/b"))
    assert branch == "trunk"


async def test_fetch_from_github_autodiscovers_and_returns_provenance(monkeypatch):
    fake = FakeGitHub([_blob("openapi.yaml")])
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    fetched = await fetch_from_github("https://github.com/acme/api")

    assert json.loads(fetched.content)["info"]["title"] == "Petstore"
    assert fetched.source_kind == "github"
    assert fetched.provenance == {
        "owner": "acme",
        "repo": "api",
        "branch": "main",
        "path": "openapi.yaml",
    }
    assert fetched.source_url.endswith("/blob/main/openapi.yaml")


async def test_explicit_path_skips_discovery(monkeypatch):
    fake = FakeGitHub([])
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    await fetch_from_github("https://github.com/a/b/blob/main/custom/spec.json")

    assert not any("/git/trees/" in path for path, _ in fake.requests)


async def test_no_spec_found_is_actionable(monkeypatch):
    fake = FakeGitHub([_blob("README.md")])
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    with pytest.raises(OpenAPIError) as exc:
        await fetch_from_github("https://github.com/acme/api")
    assert exc.value.code == "no_spec_found"
    assert "acme/api" in exc.value.message


async def test_github_token_travels_only_to_github(monkeypatch):
    """A private-repo token must appear on GitHub calls and nowhere else."""
    fake = FakeGitHub([_blob("openapi.yaml")])
    monkeypatch.setattr("sutr.openapi.sources._github_json", fake)

    await fetch_from_github("https://github.com/acme/api", token="ghp_secret")

    assert fake.requests, "no GitHub requests recorded"
    assert all(meta["token"] == "ghp_secret" for _path, meta in fake.requests)


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "github_auth"), (403, "github_rate_limited"), (404, "github_not_found")],
)
async def test_github_errors_explain_the_fix(monkeypatch, status, code):
    import httpx

    def handler(_request):
        return httpx.Response(status, json={"message": "nope"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr("sutr.openapi.sources.httpx.AsyncClient", patched)

    with pytest.raises(OpenAPIError) as exc:
        await discover_github_specs(parse_github_url("https://github.com/a/b"))
    assert exc.value.code == code


# ── SwaggerHub ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://app.swaggerhub.com/apis/acme/petstore/1.0.0",
            ("acme", "petstore", "1.0.0"),
        ),
        (
            "https://portal.swaggerhub.com/apis-docs/apilayer-863/MarketstackAPIV2/2.0.0?source=catalog",
            ("apilayer-863", "MarketstackAPIV2", "2.0.0"),
        ),
        ("https://api.swaggerhub.com/apis/acme/petstore", ("acme", "petstore", None)),
    ],
)
def test_parse_swaggerhub_url_shapes(url, expected):
    target = parse_swaggerhub_url(url)
    assert (target.owner, target.api, target.version) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://swagger.io/apis/acme/petstore/1.0.0",
        "https://app.swaggerhub.com/organizations/acme",
        "https://app.swaggerhub.com/apis/acme",
    ],
)
def test_parse_swaggerhub_url_rejects_bad_input(url):
    with pytest.raises(OpenAPIError) as exc:
        parse_swaggerhub_url(url)
    assert exc.value.code == "bad_source_url"


async def test_swaggerhub_sends_the_raw_key_and_asks_for_resolved(monkeypatch):
    """SwaggerHub wants the key raw (not Bearer), and resolved=true inlines the
    external $refs our resolver refuses."""
    seen: dict = {}

    async def fake_fetch(url, headers=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        return json.dumps(SPEC)

    monkeypatch.setattr("sutr.openapi.loader.fetch_spec_from_url", fake_fetch)

    fetched = await fetch_from_swaggerhub(
        "https://app.swaggerhub.com/apis/acme/petstore/1.0.0",
        api_key="sh_secret",
        resolved=True,
    )

    assert seen["url"] == "https://api.swaggerhub.com/apis/acme/petstore/1.0.0?resolved=true"
    assert seen["headers"]["Authorization"] == "sh_secret"  # raw, not "Bearer ..."
    assert fetched.provenance == {"owner": "acme", "api": "petstore", "version": "1.0.0"}


async def test_swaggerhub_omits_auth_when_no_key(monkeypatch):
    seen: dict = {}

    async def fake_fetch(url, headers=None):
        seen["headers"] = headers or {}
        return json.dumps(SPEC)

    monkeypatch.setattr("sutr.openapi.loader.fetch_spec_from_url", fake_fetch)
    await fetch_from_swaggerhub("https://app.swaggerhub.com/apis/a/b/1", resolved=False)
    assert "Authorization" not in seen["headers"]


# ── Source detection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://github.com/a/b", "github"),
        ("https://raw.githubusercontent.com/a/b/main/openapi.yaml", "github"),
        ("https://app.swaggerhub.com/apis/a/b/1", "swaggerhub"),
        ("https://petstore3.swagger.io/api/v3/openapi.json", "url"),
        ("", "url"),
    ],
)
def test_detect_source_kind(url, kind):
    assert detect_source_kind(url) == kind


def test_conventional_filename_list_is_ordered_by_preference():
    """openapi.* before swagger.*, YAML before JSON — the discovery ranking
    depends on this order, so pin it."""
    assert SPEC_FILENAMES[0] == "openapi.yaml"
    assert SPEC_FILENAMES.index("openapi.json") < SPEC_FILENAMES.index("swagger.yaml")
