"""Google Cloud deployment provider: Cloud Build -> Artifact Registry -> Cloud Run.

The generated package already carries a Dockerfile, so the shape is the same
one `gcloud run deploy --source` uses: upload the archive to the project's
Cloud Build staging bucket, build and push an image, then create or update a
Cloud Run service pointing at it.

Two decisions worth naming:

- **The service is made publicly invokable.** An MCP endpoint that Claude
  Desktop cannot reach is not a deployment. If an organization policy forbids
  `allUsers`, that is surfaced as a real error rather than leaving a service
  nobody can call.
- **Stop toggles ingress, it does not scale to zero.** Cloud Run is already
  scale-to-zero, so "stopped" has to mean *unreachable*; setting ingress to
  internal-only does that without destroying the revision, and start puts it
  back.

Everything here authorizes with the user's own OAuth access token, so a
deployment can only ever touch what that user could already touch.
"""

from urllib.parse import quote

from sutr.deploy.base import (
    ConfigField,
    DeploymentProvider,
    DeploySpec,
    ProviderError,
    ProviderStatus,
    ProviderTarget,
)
from sutr.deploy.cloud_http import (
    UPLOAD_TIMEOUT_SECONDS,
    poll_until,
    put_bytes,
    request_json,
)

PROVIDER = "Google Cloud"
ARTIFACT_API = "https://artifactregistry.googleapis.com/v1"
BUILD_API = "https://cloudbuild.googleapis.com/v1"
RUN_API = "https://run.googleapis.com/v2"
STORAGE_API = "https://storage.googleapis.com/storage/v1"
STORAGE_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1"
LOGGING_API = "https://logging.googleapis.com/v2"

BUILD_TIMEOUT_SECONDS = 900
ROLLOUT_TIMEOUT_SECONDS = 600
CONTAINER_PORT = 8000


class GcpProvider(DeploymentProvider):
    id = "gcp"
    display_name = "Google Cloud Run"
    connection_provider = "gcp"
    creates = (
        "An Artifact Registry repository (if missing), one Cloud Build build, "
        "and a public Cloud Run service that scales to zero."
    )
    config_fields = (
        ConfigField(
            key="project",
            label="Project",
            kind="target",
            help="The Google Cloud project the service runs in and is billed to. "
            "Only projects your connected account can see are listed.",
        ),
        ConfigField(
            key="region",
            label="Region",
            kind="region",
            default="us-central1",
            placeholder="us-central1",
            help="Cloud Run region. The Artifact Registry repository and the "
            "build run in the same region, so the image never leaves it.",
        ),
        ConfigField(
            key="repository",
            label="Artifact Registry repository",
            default="sutr",
            required=False,
            placeholder="sutr",
            help="Docker repository for the built image. Created automatically "
            "if it does not exist. Reused across deployments.",
        ),
    )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _config(self, target: ProviderTarget) -> tuple[str, str, str, str]:
        project = (target.config.get("project") or "").strip()
        region = (target.config.get("region") or "us-central1").strip()
        repository = (target.config.get("repository") or "sutr").strip()
        token = target.credentials.get("access_token", "")
        if not project:
            raise ProviderError("No Google Cloud project was chosen for this deployment.")
        if not token:
            raise ProviderError("The Google Cloud authorization is missing. Reconnect the account.")
        return project, region, repository, token

    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        if target is None:
            # Configuration-time check: the provider itself is always usable,
            # readiness depends on the connection the user picks.
            return True, None
        try:
            project, region, _repository, token = self._config(target)
        except ProviderError as exc:
            return False, str(exc)
        try:
            await request_json(
                "GET",
                f"{RUN_API}/projects/{quote(project)}/locations/{quote(region)}/services",
                provider=PROVIDER,
                token=token,
                params={"pageSize": 1},
            )
        except ProviderError as exc:
            return False, str(exc)
        return True, None

    # ── deploy ───────────────────────────────────────────────────────────────

    async def deploy(self, spec: DeploySpec) -> dict:
        project, region, repository, token = self._config(spec.target)
        short = str(spec.deployment_id)[:8]
        service = f"{_dns_name(spec.slug)}-{short}"
        image = f"{region}-docker.pkg.dev/{project}/{repository}/{_dns_name(spec.slug)}:{short}"

        await self._ensure_repository(project, region, repository, token)
        bucket, obj = await self._upload_source(project, spec, token, short)
        await self._build_image(project, region, token, bucket, obj, image)
        uri = await self._deploy_service(project, region, token, service, image, spec)
        await self._allow_public_invoke(project, region, token, service)

        return {
            "project": project,
            "region": region,
            "repository": repository,
            "image": image,
            "service": service,
            "bucket": bucket,
            "object": obj,
            "url": f"{uri}/mcp",
            "health_url": f"{uri}/health",
            "console_url": (
                f"https://console.cloud.google.com/run/detail/{region}/{service}"
                f"/metrics?project={project}"
            ),
        }

    async def _ensure_repository(
        self, project: str, region: str, repository: str, token: str
    ) -> None:
        parent = f"projects/{quote(project)}/locations/{quote(region)}"
        existing = await request_json(
            "GET",
            f"{ARTIFACT_API}/{parent}/repositories/{quote(repository)}",
            provider=PROVIDER,
            token=token,
            tolerate=(404,),
        )
        if existing is not None:
            return
        operation = await request_json(
            "POST",
            f"{ARTIFACT_API}/{parent}/repositories",
            provider=PROVIDER,
            token=token,
            params={"repositoryId": repository},
            json_body={
                "format": "DOCKER",
                "description": "Images for MCP servers generated by sutr",
            },
            # Two deployments started at once both see "missing" and both
            # create; the loser's 409 means the repository exists, which is
            # the state we wanted.
            tolerate=(409,),
        )
        if operation and operation.get("name") and not operation.get("done"):
            await poll_until(
                lambda: request_json(
                    "GET",
                    f"{ARTIFACT_API}/{operation['name']}",
                    provider=PROVIDER,
                    token=token,
                ),
                lambda result: bool(result.get("done")),
                provider=PROVIDER,
                what="creating the Artifact Registry repository",
                timeout_seconds=180,
                interval_seconds=3,
            )

    async def _upload_source(
        self, project: str, spec: DeploySpec, token: str, short: str
    ) -> tuple[str, str]:
        bucket = f"{project}_cloudbuild"
        exists = await request_json(
            "GET",
            f"{STORAGE_API}/b/{quote(bucket)}",
            provider=PROVIDER,
            token=token,
            tolerate=(404,),
        )
        if exists is None:
            await request_json(
                "POST",
                f"{STORAGE_API}/b",
                provider=PROVIDER,
                token=token,
                params={"project": project},
                json_body={"name": bucket, "storageClass": "STANDARD", "location": "US"},
                tolerate=(409,),
            )

        obj = f"sutr/{spec.slug}-{short}.zip"
        await put_bytes(
            f"{STORAGE_UPLOAD_API}/b/{quote(bucket)}/o?uploadType=media&name={quote(obj, safe='')}",
            spec.package_zip,
            provider=PROVIDER,
            token=token,
            headers={"Content-Type": "application/zip"},
            method="POST",
        )
        return bucket, obj

    async def _build_image(
        self, project: str, region: str, token: str, bucket: str, obj: str, image: str
    ) -> None:
        parent = f"projects/{quote(project)}/locations/{quote(region)}"
        operation = await request_json(
            "POST",
            f"{BUILD_API}/{parent}/builds",
            provider=PROVIDER,
            token=token,
            json_body={
                "source": {"storageSource": {"bucket": bucket, "object": obj}},
                "steps": [
                    {
                        "name": "gcr.io/cloud-builders/docker",
                        "args": ["build", "-t", image, "."],
                    }
                ],
                "images": [image],
                # Without a user-specified bucket, Cloud Build refuses to write
                # its own log bucket in some projects; Cloud Logging always works.
                "options": {"logging": "CLOUD_LOGGING_ONLY"},
                "timeout": f"{BUILD_TIMEOUT_SECONDS}s",
            },
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        build_id = ((operation or {}).get("metadata") or {}).get("build", {}).get("id")
        if not build_id:
            raise ProviderError("Cloud Build did not return a build id.")

        result = await poll_until(
            lambda: request_json(
                "GET",
                f"{BUILD_API}/{parent}/builds/{quote(build_id)}",
                provider=PROVIDER,
                token=token,
            ),
            lambda build: (
                build.get("status")
                in ("SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED")
            ),
            provider=PROVIDER,
            what="building the container image",
            timeout_seconds=BUILD_TIMEOUT_SECONDS,
        )
        if result.get("status") != "SUCCESS":
            raise ProviderError(
                f"Cloud Build finished with status {result.get('status')}. "
                f"Full log: {result.get('logUrl') or 'see the Cloud Build console'}"
            )

    async def _deploy_service(
        self,
        project: str,
        region: str,
        token: str,
        service: str,
        image: str,
        spec: DeploySpec,
    ) -> str:
        parent = f"projects/{quote(project)}/locations/{quote(region)}"
        body = {
            "ingress": "INGRESS_TRAFFIC_ALL",
            "launchStage": "GA",
            "labels": {"sutr-deployment": str(spec.deployment_id)},
            "template": {
                "containers": [
                    {
                        "image": image,
                        # The image entrypoint is `python server.py`, whose
                        # default transport is stdio; these args are what make
                        # it listen.
                        "args": [
                            "--transport",
                            "http",
                            "--host",
                            "0.0.0.0",
                            "--port",
                            str(spec.internal_port),
                        ],
                        "ports": [{"containerPort": spec.internal_port}],
                        "env": [{"name": key, "value": value} for key, value in spec.env.items()],
                        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                    }
                ],
                "scaling": {"minInstanceCount": 0, "maxInstanceCount": 2},
            },
        }
        existing = await request_json(
            "GET",
            f"{RUN_API}/{parent}/services/{quote(service)}",
            provider=PROVIDER,
            token=token,
            tolerate=(404,),
        )
        if existing is None:
            operation = await request_json(
                "POST",
                f"{RUN_API}/{parent}/services",
                provider=PROVIDER,
                token=token,
                params={"serviceId": service},
                json_body=body,
            )
        else:
            operation = await request_json(
                "PATCH",
                f"{RUN_API}/{parent}/services/{quote(service)}",
                provider=PROVIDER,
                token=token,
                json_body=body,
            )

        if operation and operation.get("name") and not operation.get("done"):
            await poll_until(
                lambda: request_json(
                    "GET", f"{RUN_API}/{operation['name']}", provider=PROVIDER, token=token
                ),
                lambda result: bool(result.get("done")),
                provider=PROVIDER,
                what="rolling out the Cloud Run service",
                timeout_seconds=ROLLOUT_TIMEOUT_SECONDS,
            )

        final = await request_json(
            "GET",
            f"{RUN_API}/{parent}/services/{quote(service)}",
            provider=PROVIDER,
            token=token,
        )
        uri = (final or {}).get("uri")
        if not uri:
            raise ProviderError("Cloud Run did not report a URL for the service.")
        return uri.rstrip("/")

    async def _allow_public_invoke(
        self, project: str, region: str, token: str, service: str
    ) -> None:
        parent = f"projects/{quote(project)}/locations/{quote(region)}"
        try:
            await request_json(
                "POST",
                f"{RUN_API}/{parent}/services/{quote(service)}:setIamPolicy",
                provider=PROVIDER,
                token=token,
                json_body={
                    "policy": {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}
                },
            )
        except ProviderError as exc:
            raise ProviderError(
                "The Cloud Run service was created but could not be made publicly "
                "invokable, so no MCP client can reach it. This is usually the "
                "'Domain restricted sharing' organization policy blocking allUsers. "
                f"Underlying error: {exc}"
            )

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _service_path(self, state: dict) -> str:
        project, region, service = state.get("project"), state.get("region"), state.get("service")
        if not project or not region or not service:
            raise ProviderError("This deployment has no Cloud Run service recorded.")
        return f"projects/{quote(project)}/locations/{quote(region)}/services/{quote(service)}"

    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus:
        token = target.credentials.get("access_token", "")
        if not token:
            return ProviderStatus(state="error", detail="No Google Cloud authorization.")
        service = await request_json(
            "GET",
            f"{RUN_API}/{self._service_path(state)}",
            provider=PROVIDER,
            token=token,
            tolerate=(404,),
        )
        if service is None:
            return ProviderStatus(state="not_found", detail="The Cloud Run service is gone.")
        if service.get("ingress") == "INGRESS_TRAFFIC_INTERNAL_ONLY":
            return ProviderStatus(state="stopped", detail="Ingress is internal-only.")
        condition = service.get("terminalCondition") or {}
        if condition.get("state") == "CONDITION_SUCCEEDED":
            return ProviderStatus(state="running")
        if condition.get("state") in ("CONDITION_FAILED", "CONDITION_RECONCILING"):
            failed = condition.get("state") == "CONDITION_FAILED"
            return ProviderStatus(
                state="error" if failed else "running",
                detail=condition.get("message"),
            )
        return ProviderStatus(state="running")

    async def _set_ingress(self, state: dict, target: ProviderTarget, ingress: str) -> None:
        token = target.credentials.get("access_token", "")
        await request_json(
            "PATCH",
            f"{RUN_API}/{self._service_path(state)}",
            provider=PROVIDER,
            token=token,
            json_body={"ingress": ingress},
        )

    async def start(self, state: dict, target: ProviderTarget) -> None:
        await self._set_ingress(state, target, "INGRESS_TRAFFIC_ALL")

    async def stop(self, state: dict, target: ProviderTarget) -> None:
        await self._set_ingress(state, target, "INGRESS_TRAFFIC_INTERNAL_ONLY")

    async def remove(self, state: dict, target: ProviderTarget) -> None:
        token = target.credentials.get("access_token", "")
        if not token or not state.get("service"):
            return
        await request_json(
            "DELETE",
            f"{RUN_API}/{self._service_path(state)}",
            provider=PROVIDER,
            token=token,
            tolerate=(404,),
        )
        bucket, obj = state.get("bucket"), state.get("object")
        if bucket and obj:
            # Best effort: a leftover staging object costs cents, and failing
            # the delete over it would strand the row.
            await request_json(
                "DELETE",
                f"{STORAGE_API}/b/{quote(bucket)}/o/{quote(obj, safe='')}",
                provider=PROVIDER,
                token=token,
                tolerate=(403, 404),
            )

    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str:
        token = target.credentials.get("access_token", "")
        project, service = state.get("project"), state.get("service")
        if not token or not project or not service:
            return ""
        payload = await request_json(
            "POST",
            f"{LOGGING_API}/entries:list",
            provider=PROVIDER,
            token=token,
            json_body={
                "resourceNames": [f"projects/{project}"],
                "filter": (
                    'resource.type="cloud_run_revision" '
                    f'AND resource.labels.service_name="{service}"'
                ),
                "orderBy": "timestamp desc",
                "pageSize": min(tail, 1000),
            },
        )
        entries = (payload or {}).get("entries", [])
        lines = []
        for entry in reversed(entries):
            text = entry.get("textPayload")
            if text is None:
                text = str(entry.get("jsonPayload") or entry.get("protoPayload") or "")
            lines.append(f"{entry.get('timestamp', '')} {text}")
        return "\n".join(lines)


def _dns_name(slug: str) -> str:
    """Cloud Run service and image names are DNS labels: lowercase, hyphens."""
    cleaned = "".join(c if c.isalnum() else "-" for c in slug.lower()).strip("-")
    return (cleaned or "mcp-server")[:40].strip("-")
