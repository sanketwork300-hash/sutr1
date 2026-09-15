"""Azure deployment provider: ACR Tasks -> Azure Container Registry -> Container Apps.

The equivalent of `az containerapp up`: hand the package's source archive to
the registry's own build service, then run the resulting image as a Container
App with external ingress.

Notes that matter when reading this:

- **ACR builds want a tar.gz**, not the zip every other consumer takes, hence
  `zip_to_tar_gz`. The upload goes to a SAS URL the registry hands out, which
  is why that one request carries no bearer token.
- **The registry admin user is enabled** so the Container App can pull with a
  username and password. A managed identity would be tidier, but it needs a
  role assignment on the subscription, which many users' accounts cannot make
  — and a deploy that fails on an IAM technicality is worse than a documented
  registry credential held in the app's own secret store.
- **Every long-running ARM call is polled on the resource itself** rather than
  through the async-operation header, because ARM reports the same terminal
  state either way and the resource read is the one we need regardless.
- **Log retrieval is a known gap.** Container Apps keeps application logs in
  Log Analytics, which is a different API with a different token audience;
  `logs()` says so and points at the portal instead of pretending.
"""

from urllib.parse import quote

from sutr.deploy.base import (
    ConfigField,
    DeploymentProvider,
    DeploySpec,
    ProviderError,
    ProviderMetrics,
    ProviderStatus,
    ProviderTarget,
)
from sutr.deploy.cloud_http import (
    UPLOAD_TIMEOUT_SECONDS,
    poll_until,
    put_bytes,
    request_json,
    zip_to_tar_gz,
)

PROVIDER = "Azure"
ARM = "https://management.azure.com"
RESOURCE_GROUP_API = "2021-04-01"
REGISTRY_API = "2023-07-01"
# The build (task run) endpoints have only ever shipped under this preview
# version; it is what `az acr build` itself calls.
REGISTRY_BUILD_API = "2019-06-01-preview"
CONTAINER_APPS_API = "2024-03-01"

BUILD_TIMEOUT_SECONDS = 1200
PROVISION_TIMEOUT_SECONDS = 900
TERMINAL_PROVISIONING = ("Succeeded", "Failed", "Canceled")
TERMINAL_RUN = ("Succeeded", "Failed", "Canceled", "Error", "Timeout")


class AzureProvider(DeploymentProvider):
    id = "azure"
    display_name = "Azure Container Apps"
    connection_provider = "azure"
    supports_update = True
    supports_metrics = True
    creates = (
        "A resource group, a container registry, a Container Apps environment "
        "(each only if missing), one image build, and a Container App with "
        "external ingress."
    )
    config_fields = (
        ConfigField(
            key="subscription",
            label="Subscription",
            kind="target",
            help="Billed for everything this deployment creates. Only "
            "subscriptions your connected account can see are listed.",
        ),
        ConfigField(
            key="location",
            label="Region",
            kind="region",
            default="eastus",
            placeholder="eastus",
            help="Azure region for the resource group, registry, and app.",
        ),
        ConfigField(
            key="resource_group",
            label="Resource group",
            default="sutr-mcp",
            required=False,
            placeholder="sutr-mcp",
            help="Created if it does not exist. Keeping every sutr deployment "
            "in one group makes them easy to find and to delete together.",
        ),
        ConfigField(
            key="registry",
            label="Container registry name",
            placeholder="acmemcp",
            help="Azure Container Registry names are GLOBALLY unique and must be "
            "5-50 lowercase alphanumeric characters with no hyphens. Pick one "
            "nobody else has; it becomes <name>.azurecr.io.",
        ),
        ConfigField(
            key="environment",
            label="Container Apps environment",
            default="sutr-mcp-env",
            required=False,
            placeholder="sutr-mcp-env",
            help="The shared environment apps run in. Created if missing; the "
            "first creation takes a few minutes.",
        ),
    )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _config(self, target: ProviderTarget) -> dict[str, str]:
        config = {
            "subscription": (target.config.get("subscription") or "").strip(),
            "location": (target.config.get("location") or "eastus").strip(),
            "resource_group": (target.config.get("resource_group") or "sutr-mcp").strip(),
            "registry": (target.config.get("registry") or "").strip().lower(),
            "environment": (target.config.get("environment") or "sutr-mcp-env").strip(),
            "token": target.credentials.get("access_token", ""),
        }
        if not config["subscription"]:
            raise ProviderError("No Azure subscription was chosen for this deployment.")
        if not config["token"]:
            raise ProviderError("The Azure authorization is missing. Reconnect the account.")
        return config

    def _rg_scope(self, config: dict[str, str]) -> str:
        return (
            f"{ARM}/subscriptions/{quote(config['subscription'])}"
            f"/resourceGroups/{quote(config['resource_group'])}"
        )

    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        if target is None:
            return True, None
        try:
            config = self._config(target)
        except ProviderError as exc:
            return False, str(exc)
        if not config["registry"]:
            return False, "A container registry name is required."
        if not config["registry"].isalnum() or not 5 <= len(config["registry"]) <= 50:
            return False, (
                "The registry name must be 5-50 alphanumeric characters with no "
                "hyphens or underscores."
            )
        try:
            await request_json(
                "GET",
                f"{ARM}/subscriptions/{quote(config['subscription'])}",
                provider=PROVIDER,
                token=config["token"],
                params={"api-version": "2022-12-01"},
            )
        except ProviderError as exc:
            return False, str(exc)
        return True, None

    # ── deploy ───────────────────────────────────────────────────────────────

    async def deploy(self, spec: DeploySpec) -> dict:
        config = self._config(spec.target)
        ok, reason = await self.available(spec.target)
        if not ok:
            raise ProviderError(reason or "The Azure target is not usable.")

        short = str(spec.deployment_id)[:8]
        app_name = f"{_dns_name(spec.slug)}-{short}"
        image_repo = _dns_name(spec.slug)
        image = f"{config['registry']}.azurecr.io/{image_repo}:{spec.artifact_tag}"

        await self._ensure_resource_group(config)
        await self._ensure_registry(config)
        relative_path = await self._upload_source(config, spec)
        await self._build_image(config, relative_path, f"{image_repo}:{spec.artifact_tag}")
        username, password = await self._registry_credentials(config)
        environment_id = await self._ensure_environment(config)
        fqdn = await self._create_app(
            config, spec, app_name, image, environment_id, username, password
        )

        return {
            "subscription": config["subscription"],
            "resource_group": config["resource_group"],
            "location": config["location"],
            "registry": config["registry"],
            "environment": config["environment"],
            "app": app_name,
            "image": image,
            "url": f"https://{fqdn}/mcp",
            "health_url": f"https://{fqdn}/health",
            "console_url": (
                f"https://portal.azure.com/#resource/subscriptions/{config['subscription']}"
                f"/resourceGroups/{config['resource_group']}/providers/Microsoft.App"
                f"/containerApps/{app_name}"
            ),
        }

    async def _ensure_resource_group(self, config: dict[str, str]) -> None:
        await request_json(
            "PUT",
            self._rg_scope(config),
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": RESOURCE_GROUP_API},
            json_body={"location": config["location"]},
        )

    def _registry_url(self, config: dict[str, str]) -> str:
        return (
            f"{self._rg_scope(config)}/providers/Microsoft.ContainerRegistry"
            f"/registries/{quote(config['registry'])}"
        )

    async def _ensure_registry(self, config: dict[str, str]) -> None:
        await request_json(
            "PUT",
            self._registry_url(config),
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": REGISTRY_API},
            json_body={
                "location": config["location"],
                "sku": {"name": "Basic"},
                "properties": {"adminUserEnabled": True},
            },
        )
        await self._await_provisioned(
            self._registry_url(config), config, {"api-version": REGISTRY_API}, "the registry"
        )

    async def _upload_source(self, config: dict[str, str], spec: DeploySpec) -> str:
        upload = await request_json(
            "POST",
            f"{self._registry_url(config)}/listBuildSourceUploadUrl",
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": REGISTRY_BUILD_API},
        )
        upload_url = (upload or {}).get("uploadUrl")
        relative_path = (upload or {}).get("relativePath")
        if not upload_url or not relative_path:
            raise ProviderError("The container registry did not return an upload location.")
        await put_bytes(
            upload_url,
            zip_to_tar_gz(spec.package_zip),
            provider=PROVIDER,
            # A SAS URL carries its own authorization in the query string;
            # attaching a bearer token here makes Azure Storage reject it.
            token=None,
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip"},
        )
        return relative_path

    async def _build_image(
        self, config: dict[str, str], relative_path: str, image_name: str
    ) -> None:
        run = await request_json(
            "POST",
            f"{self._registry_url(config)}/scheduleRun",
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": REGISTRY_BUILD_API},
            json_body={
                "type": "DockerBuildRequest",
                "sourceLocation": relative_path,
                "dockerFilePath": "Dockerfile",
                "imageNames": [image_name],
                "isPushEnabled": True,
                "platform": {"os": "Linux", "architecture": "amd64"},
            },
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        run_name = (run or {}).get("name")
        if not run_name:
            raise ProviderError("The container registry did not schedule a build.")

        result = await poll_until(
            lambda: request_json(
                "GET",
                f"{self._registry_url(config)}/runs/{quote(run_name)}",
                provider=PROVIDER,
                token=config["token"],
                params={"api-version": REGISTRY_BUILD_API},
            ),
            lambda payload: (payload.get("properties") or {}).get("status") in TERMINAL_RUN,
            provider=PROVIDER,
            what="building the container image",
            timeout_seconds=BUILD_TIMEOUT_SECONDS,
        )
        status = (result.get("properties") or {}).get("status")
        if status != "Succeeded":
            raise ProviderError(
                f"The image build finished with status {status}. Open the registry's "
                f"Tasks blade and inspect run {run_name} for the build log."
            )

    async def _registry_credentials(self, config: dict[str, str]) -> tuple[str, str]:
        payload = await request_json(
            "POST",
            f"{self._registry_url(config)}/listCredentials",
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": REGISTRY_API},
        )
        passwords = (payload or {}).get("passwords") or []
        if not payload or not payload.get("username") or not passwords:
            raise ProviderError(
                "Could not read the registry credentials. Check that the admin "
                "user is enabled on the registry."
            )
        return payload["username"], passwords[0].get("value", "")

    def _environment_url(self, config: dict[str, str]) -> str:
        return (
            f"{self._rg_scope(config)}/providers/Microsoft.App"
            f"/managedEnvironments/{quote(config['environment'])}"
        )

    async def _ensure_environment(self, config: dict[str, str]) -> str:
        existing = await request_json(
            "GET",
            self._environment_url(config),
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": CONTAINER_APPS_API},
            tolerate=(404,),
        )
        if existing is None:
            await request_json(
                "PUT",
                self._environment_url(config),
                provider=PROVIDER,
                token=config["token"],
                params={"api-version": CONTAINER_APPS_API},
                json_body={"location": config["location"], "properties": {}},
            )
        final = await self._await_provisioned(
            self._environment_url(config),
            config,
            {"api-version": CONTAINER_APPS_API},
            "the Container Apps environment",
        )
        environment_id = final.get("id")
        if not environment_id:
            raise ProviderError("Azure did not return the Container Apps environment id.")
        return environment_id

    def _app_url(self, subscription: str, resource_group: str, app: str) -> str:
        return (
            f"{ARM}/subscriptions/{quote(subscription)}/resourceGroups/{quote(resource_group)}"
            f"/providers/Microsoft.App/containerApps/{quote(app)}"
        )

    async def _create_app(
        self,
        config: dict[str, str],
        spec: DeploySpec,
        app_name: str,
        image: str,
        environment_id: str,
        username: str,
        password: str,
    ) -> str:
        # Runtime secrets become Container Apps secrets and are referenced by
        # name, so they never appear in the template as literals.
        secrets = [{"name": "sutr-registry-password", "value": password}]
        env_entries = []
        for index, (key, value) in enumerate(spec.env.items()):
            secret_name = f"sutr-env-{index}"
            secrets.append({"name": secret_name, "value": value})
            env_entries.append({"name": key, "secretRef": secret_name})

        url = self._app_url(config["subscription"], config["resource_group"], app_name)
        await request_json(
            "PUT",
            url,
            provider=PROVIDER,
            token=config["token"],
            params={"api-version": CONTAINER_APPS_API},
            json_body={
                "location": config["location"],
                "tags": {"sutr-deployment": str(spec.deployment_id)},
                "properties": {
                    "managedEnvironmentId": environment_id,
                    "configuration": {
                        "ingress": {
                            "external": True,
                            "targetPort": spec.internal_port,
                            "transport": "auto",
                        },
                        "registries": [
                            {
                                "server": f"{config['registry']}.azurecr.io",
                                "username": username,
                                "passwordSecretRef": "sutr-registry-password",
                            }
                        ],
                        "secrets": secrets,
                    },
                    "template": {
                        "containers": [
                            {
                                "name": _dns_name(spec.slug),
                                "image": image,
                                # `python server.py` defaults to stdio; these
                                # args are what make it listen on a port.
                                "args": [
                                    "--transport",
                                    "http",
                                    "--host",
                                    "0.0.0.0",
                                    "--port",
                                    str(spec.internal_port),
                                ],
                                "env": env_entries,
                                "resources": {"cpu": 0.5, "memory": "1Gi"},
                            }
                        ],
                        "scale": {"minReplicas": 0, "maxReplicas": 2},
                    },
                },
            },
        )
        final = await self._await_provisioned(
            url, config, {"api-version": CONTAINER_APPS_API}, "the Container App"
        )
        fqdn = (
            ((final.get("properties") or {}).get("configuration") or {}).get("ingress") or {}
        ).get("fqdn")
        if not fqdn:
            raise ProviderError("Azure did not return a hostname for the Container App.")
        return fqdn

    async def _await_provisioned(
        self, url: str, config: dict[str, str], params: dict[str, str], what: str
    ) -> dict:
        result = await poll_until(
            lambda: request_json(
                "GET", url, provider=PROVIDER, token=config["token"], params=params
            ),
            lambda payload: (
                (payload.get("properties") or {}).get("provisioningState") in TERMINAL_PROVISIONING
            ),
            provider=PROVIDER,
            what=f"provisioning {what}",
            timeout_seconds=PROVISION_TIMEOUT_SECONDS,
        )
        state = (result.get("properties") or {}).get("provisioningState")
        if state != "Succeeded":
            raise ProviderError(f"Azure reported {state} while provisioning {what}.")
        return result

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _state_app_url(self, state: dict) -> str:
        subscription = state.get("subscription")
        resource_group = state.get("resource_group")
        app = state.get("app")
        if not subscription or not resource_group or not app:
            raise ProviderError("This deployment has no Container App recorded.")
        return self._app_url(subscription, resource_group, app)

    async def metrics(self, state: dict, target: ProviderTarget) -> ProviderMetrics:
        """What the Container App resource itself reports.

        CPU, memory, request counts and latencies live in Azure Monitor — a
        different API with a different token audience than the deploy grant
        covers, the same situation as this provider's logs. Rather than
        inventing numbers, this reports the configured replica range and
        readiness, and names what is missing.
        """
        token = target.credentials.get("access_token", "")
        if not token:
            return ProviderMetrics(
                source="container apps", unavailable_reason="No Azure authorization."
            )
        payload = await request_json(
            "GET",
            self._state_app_url(state),
            provider=PROVIDER,
            token=token,
            params={"api-version": CONTAINER_APPS_API},
            tolerate=(404,),
        )
        if payload is None:
            return ProviderMetrics(
                source="container apps", unavailable_reason="The Container App is gone."
            )
        properties = payload.get("properties") or {}
        scale = ((properties.get("template") or {}).get("scale")) or {}
        return ProviderMetrics(
            healthy=properties.get("provisioningState") == "Succeeded",
            replicas=scale.get("minReplicas"),
            source="container apps resource",
            unavailable_reason=(
                "CPU, memory, request counts and latencies require Azure Monitor "
                "(management.azure.com/.../providers/microsoft.insights/metrics), which needs "
                "a separate authorization scope."
            ),
        )

    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus:
        token = target.credentials.get("access_token", "")
        if not token:
            return ProviderStatus(state="error", detail="No Azure authorization.")
        payload = await request_json(
            "GET",
            self._state_app_url(state),
            provider=PROVIDER,
            token=token,
            params={"api-version": CONTAINER_APPS_API},
            tolerate=(404,),
        )
        if payload is None:
            return ProviderStatus(state="not_found", detail="The Container App is gone.")
        properties = payload.get("properties") or {}
        provisioning = properties.get("provisioningState")
        if provisioning == "Failed":
            return ProviderStatus(state="error", detail="Provisioning failed.")
        scale = ((properties.get("template") or {}).get("scale")) or {}
        if scale.get("maxReplicas") == 0:
            return ProviderStatus(state="stopped", detail="The app is scaled to zero replicas.")
        if provisioning in ("Succeeded", "Updating"):
            return ProviderStatus(state="running")
        return ProviderStatus(state="error", detail=f"Provisioning state {provisioning}.")

    async def start(self, state: dict, target: ProviderTarget) -> None:
        await request_json(
            "POST",
            f"{self._state_app_url(state)}/start",
            provider=PROVIDER,
            token=target.credentials.get("access_token", ""),
            params={"api-version": CONTAINER_APPS_API},
        )

    async def stop(self, state: dict, target: ProviderTarget) -> None:
        await request_json(
            "POST",
            f"{self._state_app_url(state)}/stop",
            provider=PROVIDER,
            token=target.credentials.get("access_token", ""),
            params={"api-version": CONTAINER_APPS_API},
        )

    async def remove(self, state: dict, target: ProviderTarget) -> None:
        token = target.credentials.get("access_token", "")
        if not token or not state.get("app"):
            return
        # Only the app is removed. The resource group, registry, and
        # environment are shared across deployments, so tearing them down with
        # one deployment would break the others.
        await request_json(
            "DELETE",
            self._state_app_url(state),
            provider=PROVIDER,
            token=token,
            params={"api-version": CONTAINER_APPS_API},
            tolerate=(204, 404),
        )

    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str:
        """Container Apps keeps application logs outside ARM.

        They live in the environment's Log Analytics workspace, which is a
        separate API with a separate token audience that this connection's
        grant does not cover. Rather than return an empty string that reads as
        "no output", say where the logs actually are.
        """
        console = state.get("console_url", "")
        return (
            "Azure Container Apps streams application logs to the environment's "
            "Log Analytics workspace, which sutr's Azure authorization does not "
            "cover.\n\nView them in the portal under the app's 'Log stream' or "
            "'Logs' blade:\n" + (console or "https://portal.azure.com")
        )


def _dns_name(slug: str) -> str:
    """Container App names are DNS labels: lowercase, hyphens, 2-32 chars."""
    cleaned = "".join(c if c.isalnum() else "-" for c in slug.lower()).strip("-")
    return (cleaned or "mcp-server")[:23].strip("-")
