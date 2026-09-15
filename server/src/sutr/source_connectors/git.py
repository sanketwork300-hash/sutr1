"""GitHub as a source of truth.

The LLD's rationale (§3.3) for connectors at all: *"enterprises won't upload
800 YAML files"* — the definition already lives in a repository, so the
platform should read it from there and keep reading it.

Change detection is by **commit SHA**, not by content hash. A commit is the
repository's own answer to "has this changed?", it is one cheap API call away,
and it distinguishes "the file moved" from "the repository moved" without
transferring the file at all.

The fetching itself reuses `openapi/sources.py`, which already handles URL
shapes, private repositories, discovery, and the size limit. This connector
adds the parts sync needs: a cheap change check and structured provenance.
"""

from datetime import datetime, timezone
from urllib.parse import quote

from sutr.openapi import sources
from sutr.openapi.errors import OpenAPIError
from sutr.source_connectors.base import (
    WATCH_POLL,
    ConfigField,
    ConnectionResult,
    ConnectorError,
    Discovered,
    FetchResult,
    Provenance,
    SourceConnector,
    WatchPlan,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GitHubConnector(SourceConnector):
    id = "github"
    display_name = "GitHub repository"
    description = (
        "A specification in a repository. Watched by commit SHA, so a poll that finds "
        "nothing costs one small API call."
    )
    supports_watch = True
    supports_discovery = True
    config_fields = (
        ConfigField(
            key="url",
            label="Repository or file URL",
            kind="url",
            placeholder="https://github.com/acme/api",
        ),
        ConfigField(
            key="path",
            label="Path to the specification",
            required=False,
            help="Left empty, the repository is searched for a conventional filename.",
        ),
        ConfigField(key="branch", label="Branch", required=False),
        ConfigField(
            key="connection_id",
            label="Connected GitHub account",
            required=False,
            help="Needed for private repositories.",
        ),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        url = (config.get("url") or "").strip()
        if not url:
            return ConnectionResult(
                connected=False, message="A repository or file URL is required."
            )
        try:
            target = sources.parse_github_url(url)
        except OpenAPIError as exc:
            return ConnectionResult(connected=False, message=exc.message)
        token = secrets.get("token")
        try:
            info = await sources._github_json(
                f"/repos/{quote(target.owner)}/{quote(target.repo)}", token
            )
        except OpenAPIError as exc:
            return ConnectionResult(connected=False, message=exc.message)
        return ConnectionResult(
            connected=True,
            message=f"Connected to {target.owner}/{target.repo}.",
            detail={
                "default_branch": info.get("default_branch"),
                "private": bool(info.get("private")),
            },
        )

    async def discover(self, config: dict, secrets: dict) -> list[Discovered]:
        url = (config.get("url") or "").strip()
        if not url:
            return []
        target = sources.parse_github_url(url)
        candidates, branch = await sources.discover_github_specs(target, secrets.get("token"))
        return [
            Discovered(
                identifier=candidate.path,
                label=candidate.path,
                detail={"filename": candidate.filename, "size": candidate.size, "branch": branch},
            )
            for candidate in candidates
        ]

    async def current_version(self, config: dict, secrets: dict) -> str | None:
        """The commit SHA that last touched the watched file.

        One call, no file transfer. This is what makes polling a repository
        reasonable rather than rude.
        """
        url = (config.get("url") or "").strip()
        if not url:
            return None
        target = sources.parse_github_url(url)
        path = config.get("path") or target.path
        branch = config.get("branch") or target.branch
        token = secrets.get("token")
        try:
            if not branch:
                info = await sources._github_json(
                    f"/repos/{quote(target.owner)}/{quote(target.repo)}", token
                )
                branch = info.get("default_branch") or "main"
            query = f"?sha={quote(branch, safe='')}&per_page=1"
            if path:
                query += f"&path={quote(path)}"
            commits = await sources._github_json(
                f"/repos/{quote(target.owner)}/{quote(target.repo)}/commits{query}", token
            )
        except OpenAPIError:
            return None
        if isinstance(commits, list) and commits:
            sha = commits[0].get("sha")
            return sha if isinstance(sha, str) else None
        return None

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        url = (config.get("url") or "").strip()
        if not url:
            raise ConnectorError("no_url", "This source has no repository URL configured.")
        token = secrets.get("token")

        sha = await self.current_version(config, secrets)
        if known and known.commit_sha and sha and known.commit_sha == sha:
            provenance = known
            provenance.retrieved_at = _now()
            return FetchResult(content=None, provenance=provenance, not_modified=True)

        try:
            fetched = await sources.fetch_from_github(url, path=config.get("path"), token=token)
        except OpenAPIError as exc:
            raise ConnectorError(exc.code, exc.message)

        return FetchResult(
            content=fetched.content,
            provenance=Provenance(
                source_type=self.id,
                source_uri=fetched.source_url,
                source_version=sha,
                commit_sha=sha,
                retrieved_at=_now(),
                detail=dict(fetched.provenance),
            ),
        )

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_POLL,
            interval_seconds=900,
            conditional=True,
            reason=(
                "Polled by commit SHA — one small API call, no file transfer, unless the "
                "file actually moved. A push webhook can trigger a check immediately."
            ),
        )


class SwaggerHubConnector(SourceConnector):
    id = "swaggerhub"
    display_name = "SwaggerHub"
    description = "An API managed in SwaggerHub, for teams already keeping definitions there."
    supports_watch = True
    config_fields = (
        ConfigField(
            key="url",
            label="SwaggerHub API URL",
            kind="url",
            placeholder="https://app.swaggerhub.com/apis/acme/pets/1.0.0",
        ),
        ConfigField(
            key="resolved",
            label="Ask SwaggerHub to inline external references",
            kind="select",
            required=False,
            options=("true", "false"),
            help="Useful because external $refs are refused by the resolver.",
        ),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        url = (config.get("url") or "").strip()
        if not url:
            return ConnectionResult(connected=False, message="A SwaggerHub API URL is required.")
        try:
            result = await self.fetch(config, secrets)
        except ConnectorError as exc:
            return ConnectionResult(connected=False, message=exc.message)
        validation = self.validate(result.content or "")
        if not validation.valid:
            return ConnectionResult(connected=False, message=validation.message)
        return ConnectionResult(
            connected=True,
            message=f"Found {validation.api_title} {validation.api_version}.",
        )

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        url = (config.get("url") or "").strip()
        if not url:
            raise ConnectorError("no_url", "This source has no SwaggerHub URL configured.")
        try:
            fetched = await sources.fetch_from_swaggerhub(
                url,
                api_key=secrets.get("api_key"),
                resolved=str(config.get("resolved", "")).lower() == "true",
            )
        except OpenAPIError as exc:
            raise ConnectorError(exc.code, exc.message)
        version = fetched.provenance.get("version")
        return FetchResult(
            content=fetched.content,
            provenance=Provenance(
                source_type=self.id,
                source_uri=fetched.source_url,
                source_version=version,
                retrieved_at=_now(),
                detail=dict(fetched.provenance),
            ),
        )

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_POLL,
            interval_seconds=3600,
            # SwaggerHub's definition endpoint does not answer conditional
            # requests, so every poll transfers the document and the change is
            # found by hashing it. Said plainly rather than implied by a
            # slower interval.
            conditional=False,
            reason=(
                "Polled by re-fetching: the SwaggerHub API does not support conditional "
                "requests, so each check transfers the definition."
            ),
        )
