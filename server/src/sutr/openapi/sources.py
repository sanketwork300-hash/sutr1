"""Where a specification comes from: GitHub, SwaggerHub, or a plain URL.

Source adapters are deliberately kept outside the compiler pipeline (the shape
`apitomcp` established): each one's only job is to turn a user-supplied
locator into raw spec text plus provenance, so `normalize()` never learns
where a document came from.

Every outbound request goes through `loader.fetch_spec_from_url`, which is
SSRF-screened, redirect-refusing, and size-capped. Host allowlists here are a
second layer, not the only one: a GitHub token must never be sent anywhere but
GitHub.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.limits import MAX_SPEC_BYTES, URL_FETCH_TIMEOUT_SECONDS

GITHUB_API = "https://api.github.com"
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "raw.githubusercontent.com"})
SWAGGERHUB_API = "https://api.swaggerhub.com"
SWAGGERHUB_HOSTS = frozenset({"app.swaggerhub.com", "portal.swaggerhub.com", "api.swaggerhub.com"})

# Conventional specification filenames, in the order we prefer them when a
# repository contains several.
SPEC_FILENAMES = (
    "openapi.yaml",
    "openapi.yml",
    "openapi.json",
    "swagger.yaml",
    "swagger.yml",
    "swagger.json",
)
SPEC_SUFFIXES = (".yaml", ".yml", ".json")
# A repository tree can be enormous; only this many entries are scanned.
MAX_TREE_ENTRIES = 20_000
MAX_CANDIDATES = 50


@dataclass
class SpecCandidate:
    """One specification file discovered in a repository."""

    path: str
    filename: str
    size: int | None = None
    # Lower sorts first — see `_rank_candidate` for the rule.
    rank: tuple[int, int, int, str] = field(default=(9, 9, 9, ""), compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "filename": self.filename, "size": self.size}


@dataclass
class FetchedSpec:
    """Raw specification text plus where it came from."""

    content: str
    source_kind: str
    source_url: str
    provenance: dict[str, Any] = field(default_factory=dict)


# ── GitHub ───────────────────────────────────────────────────────────────────


@dataclass
class GitHubTarget:
    owner: str
    repo: str
    branch: str | None = None
    # A specific file to import (from a /blob/ or raw URL).
    path: str | None = None
    # A directory to search within (from a /tree/ URL). Distinct from `path`
    # because a directory must never be fetched as a file.
    directory: str | None = None


def parse_github_url(url: str) -> GitHubTarget:
    """Parse the GitHub URL shapes people actually paste.

    https://github.com/owner/repo
    https://github.com/owner/repo/tree/main/dir
    https://github.com/owner/repo/blob/main/dir/openapi.yaml
    https://raw.githubusercontent.com/owner/repo/main/dir/openapi.yaml
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise OpenAPIError("bad_source_url", "The URL must start with http:// or https://.")
    host = parsed.netloc.lower()
    if host not in GITHUB_HOSTS:
        raise OpenAPIError(
            "bad_source_url",
            f"'{host or url}' is not a GitHub URL. Paste a repository, file, or raw URL.",
        )

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise OpenAPIError(
            "bad_source_url",
            "The URL must include an owner and repository, e.g. https://github.com/owner/repo.",
        )

    owner, repo = parts[0], parts[1].removesuffix(".git")

    if host == "raw.githubusercontent.com":
        # /owner/repo/branch/path...
        if len(parts) < 4:
            raise OpenAPIError("bad_source_url", "A raw URL must include a branch and file path.")
        return GitHubTarget(owner, repo, branch=parts[2], path="/".join(parts[3:]))

    if len(parts) >= 4 and parts[2] in {"blob", "tree", "raw"}:
        branch = parts[3]
        rest = "/".join(parts[4:]) or None
        if parts[2] == "tree":
            # A directory: a search root for discovery, never a file to fetch.
            return GitHubTarget(owner, repo, branch=branch, directory=rest)
        return GitHubTarget(owner, repo, branch=branch, path=rest)

    return GitHubTarget(owner, repo)


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sutr-openapi-import",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _github_json(path: str, token: str | None) -> Any:
    """GET a GitHub API path and parse JSON, mapping failures to OpenAPIError."""
    url = f"{GITHUB_API}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=URL_FETCH_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.get(url, headers=_github_headers(token))
    except httpx.HTTPError as exc:
        raise OpenAPIError("fetch_failed", f"Could not reach GitHub: {exc}")

    if response.status_code == 401:
        raise OpenAPIError("github_auth", "GitHub rejected the token. Check that it is valid.")
    if response.status_code == 403:
        raise OpenAPIError(
            "github_rate_limited",
            "GitHub refused the request (rate limit or private repository). "
            "Supply a personal access token with 'repo' read scope.",
        )
    if response.status_code == 404:
        raise OpenAPIError(
            "github_not_found",
            "GitHub returned 404. Check the owner, repository, and branch — "
            "private repositories need a token.",
        )
    if response.status_code >= 400:
        raise OpenAPIError("fetch_failed", f"GitHub returned HTTP {response.status_code}.")

    try:
        return response.json()
    except ValueError:
        raise OpenAPIError("fetch_failed", "GitHub returned a response that was not JSON.")


def _rank_candidate(path: str) -> tuple[int, int, int, str]:
    """Ranking rule, in words: an `openapi.*` file beats a `swagger.*` one
    (swagger.json is often a generated artifact), a shallower path beats a
    deeper one, and YAML beats JSON. Anything merely spec-shaped comes last.

    This only chooses the *default* selection — the wizard lists every
    candidate and the user can pick another.
    """
    filename = path.rsplit("/", 1)[-1].lower()
    depth = path.count("/")
    if filename in SPEC_FILENAMES:
        family = 0 if filename.startswith("openapi") else 1
        return (family, depth, SPEC_FILENAMES.index(filename), path)
    # Names like "petstore.openapi.yaml" or "api-spec.json".
    return (2, depth, len(SPEC_FILENAMES), path)


def _looks_like_spec(path: str) -> bool:
    filename = path.rsplit("/", 1)[-1].lower()
    if filename in SPEC_FILENAMES:
        return True
    if not filename.endswith(SPEC_SUFFIXES):
        return False
    return any(hint in filename for hint in ("openapi", "swagger", "api-spec", "apispec"))


async def discover_github_specs(
    target: GitHubTarget, token: str | None = None
) -> tuple[list[SpecCandidate], str]:
    """List candidate spec files in a repository. Returns (candidates, branch)."""
    branch = target.branch
    if not branch:
        repo_info = await _github_json(f"/repos/{quote(target.owner)}/{quote(target.repo)}", token)
        branch = repo_info.get("default_branch") or "main"

    tree = await _github_json(
        f"/repos/{quote(target.owner)}/{quote(target.repo)}/git/trees/"
        f"{quote(branch, safe='')}?recursive=1",
        token,
    )
    entries = tree.get("tree") or []
    if tree.get("truncated"):
        # Still usable — say so via the candidate list being partial.
        entries = entries[:MAX_TREE_ENTRIES]

    root = (target.directory or "").strip("/")
    candidates: list[SpecCandidate] = []
    for entry in entries[:MAX_TREE_ENTRIES]:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        if root and not path.startswith(f"{root}/") and path != root:
            continue
        if not _looks_like_spec(path):
            continue
        candidates.append(
            SpecCandidate(
                path=path,
                filename=path.rsplit("/", 1)[-1],
                size=entry.get("size"),
                rank=_rank_candidate(path),
            )
        )

    candidates.sort(key=lambda c: c.rank)
    return candidates[:MAX_CANDIDATES], branch


async def list_github_repositories(
    token: str, *, query: str = "", limit: int = 60
) -> list[dict[str, Any]]:
    """Repositories the token's owner can read, newest activity first.

    Only reachable with a connected account: an anonymous caller has no "my
    repositories" to list. `query` filters client-side on the page we fetched
    rather than calling the search API, because search is separately
    rate-limited and ranks by relevance, which reorders a list the user is
    scanning by recency.
    """
    if not token:
        raise OpenAPIError(
            "github_auth",
            "Listing repositories needs a connected GitHub account or a token.",
        )
    repos = await _github_json(
        "/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member",
        token,
    )
    if not isinstance(repos, list):
        raise OpenAPIError("fetch_failed", "GitHub returned an unexpected repository list.")

    needle = query.strip().lower()
    entries: list[dict[str, Any]] = []
    for repo in repos:
        full_name = repo.get("full_name") or ""
        if needle and needle not in full_name.lower():
            continue
        entries.append(
            {
                "full_name": full_name,
                "html_url": repo.get("html_url") or f"https://github.com/{full_name}",
                "private": bool(repo.get("private")),
                "default_branch": repo.get("default_branch") or "main",
                "description": repo.get("description") or "",
                "updated_at": repo.get("updated_at"),
            }
        )
        if len(entries) >= limit:
            break
    return entries


async def fetch_github_file(
    target: GitHubTarget, path: str, branch: str, token: str | None = None
) -> str:
    """Fetch one file's text through the contents API (works for private repos)."""
    data = await _github_json(
        f"/repos/{quote(target.owner)}/{quote(target.repo)}/contents/"
        f"{quote(path)}?ref={quote(branch, safe='')}",
        token,
    )
    if isinstance(data, list):
        raise OpenAPIError(
            "not_a_file", f"'{path}' is a directory. Choose a specification file inside it."
        )
    if data.get("size") and data["size"] > MAX_SPEC_BYTES:
        raise OpenAPIError(
            "spec_too_large",
            f"'{path}' is larger than the {MAX_SPEC_BYTES // (1024 * 1024)} MiB limit.",
        )

    encoding = data.get("encoding")
    content = data.get("content")
    if encoding == "base64" and isinstance(content, str):
        import base64

        try:
            raw = base64.b64decode(content)
        except ValueError:
            raise OpenAPIError("fetch_failed", "GitHub returned undecodable file content.")
        if len(raw) > MAX_SPEC_BYTES:
            raise OpenAPIError("spec_too_large", "The specification exceeds the size limit.")
        return raw.decode("utf-8", errors="replace")

    # Large files come back without inline content; fall back to the raw URL.
    download_url = data.get("download_url")
    if not download_url:
        raise OpenAPIError("fetch_failed", "GitHub did not return the file content.")
    from sutr.openapi.loader import fetch_spec_from_url

    return await fetch_spec_from_url(download_url, headers=_github_headers(token))


async def fetch_from_github(
    url: str, *, path: str | None = None, token: str | None = None
) -> FetchedSpec:
    """Resolve a GitHub locator to spec text, auto-discovering the file if needed."""
    target = parse_github_url(url)
    chosen = path or target.path
    branch = target.branch

    if not chosen:
        candidates, branch = await discover_github_specs(target, token)
        if not candidates:
            raise OpenAPIError(
                "no_spec_found",
                f"No OpenAPI or Swagger file found in {target.owner}/{target.repo}. "
                "Point at the file directly, or pass its path.",
            )
        chosen = candidates[0].path
    elif not branch:
        repo_info = await _github_json(f"/repos/{quote(target.owner)}/{quote(target.repo)}", token)
        branch = repo_info.get("default_branch") or "main"

    content = await fetch_github_file(target, chosen, branch, token)
    return FetchedSpec(
        content=content,
        source_kind="github",
        source_url=f"https://github.com/{target.owner}/{target.repo}/blob/{branch}/{chosen}",
        provenance={
            "owner": target.owner,
            "repo": target.repo,
            "branch": branch,
            "path": chosen,
        },
    )


# ── SwaggerHub ───────────────────────────────────────────────────────────────


@dataclass
class SwaggerHubTarget:
    owner: str
    api: str
    version: str | None = None


def parse_swaggerhub_url(url: str) -> SwaggerHubTarget:
    """Parse the SwaggerHub URL shapes people paste.

    https://app.swaggerhub.com/apis/{owner}/{api}/{version}
    https://portal.swaggerhub.com/apis-docs/{owner}/{api}/{version}
    https://api.swaggerhub.com/apis/{owner}/{api}          (version optional)
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise OpenAPIError("bad_source_url", "The URL must start with http:// or https://.")
    host = parsed.netloc.lower()
    if host not in SWAGGERHUB_HOSTS:
        raise OpenAPIError(
            "bad_source_url",
            f"'{host or url}' is not a SwaggerHub URL. Copy the URL of the API page.",
        )

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts or parts[0] not in {"apis", "apis-docs"}:
        raise OpenAPIError(
            "bad_source_url",
            "A SwaggerHub URL looks like https://app.swaggerhub.com/apis/{owner}/{api}/{version}.",
        )
    if len(parts) < 3:
        raise OpenAPIError(
            "bad_source_url", "The SwaggerHub URL must include both an owner and an API name."
        )

    version = parts[3] if len(parts) >= 4 else None
    return SwaggerHubTarget(owner=parts[1], api=parts[2], version=version)


async def fetch_from_swaggerhub(
    url: str, *, api_key: str | None = None, resolved: bool = False
) -> FetchedSpec:
    """Fetch a definition from the SwaggerHub registry.

    SwaggerHub expects the API key **raw** in the Authorization header, not as
    a Bearer token. `resolved=True` asks SwaggerHub to inline external `$ref`s,
    which is useful because our resolver refuses external references outright.
    """
    target = parse_swaggerhub_url(url)
    path = f"/apis/{quote(target.owner)}/{quote(target.api)}"
    if target.version:
        path += f"/{quote(target.version, safe='')}"
    query = "?resolved=true" if resolved else ""

    headers = {"Accept": "application/json", "User-Agent": "sutr-openapi-import"}
    if api_key:
        headers["Authorization"] = api_key

    from sutr.openapi.loader import fetch_spec_from_url

    try:
        content = await fetch_spec_from_url(f"{SWAGGERHUB_API}{path}{query}", headers=headers)
    except OpenAPIError as exc:
        if exc.code == "fetch_failed" and "401" in exc.message:
            raise OpenAPIError(
                "swaggerhub_auth",
                "SwaggerHub rejected the request. Private APIs need an API key "
                "(SwaggerHub → Settings → API Key).",
            )
        raise

    return FetchedSpec(
        content=content,
        source_kind="swaggerhub",
        source_url=url,
        provenance={"owner": target.owner, "api": target.api, "version": target.version},
    )


def detect_source_kind(url: str) -> str:
    """Best-effort guess so the UI can pick a source from a pasted URL."""
    try:
        host = urlsplit(url.strip()).netloc.lower()
    except ValueError:
        return "url"
    if host in GITHUB_HOSTS:
        return "github"
    if host in SWAGGERHUB_HOSTS:
        return "swaggerhub"
    return "url"


def spec_preview(content: str) -> dict[str, Any]:
    """Cheap title/version peek for the UI, without running the full parser."""
    try:
        data = json.loads(content)
    except ValueError:
        return {}
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return {}
    return {"title": info.get("title"), "version": info.get("version")}
