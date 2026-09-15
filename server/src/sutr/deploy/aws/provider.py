"""AWS deployment provider: S3 -> CodeBuild -> ECR -> App Runner.

AWS has no managed "build this Dockerfile and run it" service, so the pipeline
is assembled from four: the package lands in S3, CodeBuild builds and pushes
the image to ECR, and App Runner runs it behind a public HTTPS URL.

Two IAM roles are unavoidable and are therefore asked for explicitly rather
than guessed at:

- a **CodeBuild service role** that can read the S3 object, write CloudWatch
  logs, and push to ECR;
- an **App Runner ECR access role** that App Runner assumes to pull the image.

Both are one-time setup per account. Inventing names for them and failing
cryptically when they do not exist would be worse than asking.

The image's entrypoint is ``python server.py``, whose default transport is
stdio. App Runner's ``StartCommand`` semantics with respect to an existing
ENTRYPOINT are ambiguous, so the buildspec re-tags the image with an explicit
``CMD`` instead: unambiguous, and it makes the published image runnable as-is.
"""

from typing import Any

from sutr.deploy.aws.client import AwsClient, AwsError
from sutr.deploy.base import (
    ConfigField,
    DeploymentProvider,
    DeploySpec,
    ProviderError,
    ProviderMetrics,
    ProviderStatus,
    ProviderTarget,
)
from sutr.deploy.cloud_http import poll_until

PROVIDER = "AWS"
BUILD_TIMEOUT_SECONDS = 1200
SERVICE_TIMEOUT_SECONDS = 900

# ruff: noqa: E501 - a buildspec is YAML AWS parses verbatim; wrapping a
# command would change what runs.
BUILDSPEC = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $SUTR_REGISTRY
  build:
    commands:
      - docker build -t $SUTR_IMAGE_URI .
      - printf 'FROM %s\\nCMD ["--transport","http","--host","0.0.0.0","--port","8000"]\\n' "$SUTR_IMAGE_URI" > Dockerfile.serve
      - docker build -t $SUTR_IMAGE_URI -f Dockerfile.serve .
  post_build:
    commands:
      - docker push $SUTR_IMAGE_URI
"""

_TERMINAL_BUILD = ("SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED")
_TERMINAL_SERVICE = ("RUNNING", "CREATE_FAILED", "DELETED", "PAUSED")


class AwsProvider(DeploymentProvider):
    id = "aws"
    display_name = "AWS App Runner"
    connection_provider = "aws"
    supports_update = True
    supports_metrics = True
    creates = (
        "An S3 bucket and ECR repository (if missing), a CodeBuild project and "
        "one build, and an App Runner service with a public HTTPS URL."
    )
    config_fields = (
        ConfigField(
            key="account",
            label="Account",
            kind="target",
            help="The AWS account to deploy into. Only accounts your IAM "
            "Identity Center identity is assigned to are listed.",
        ),
        ConfigField(
            key="role",
            label="Permission set / role",
            kind="role",
            help="The Identity Center role sutr assumes. It needs permission for "
            "S3, ECR, CodeBuild, App Runner, IAM PassRole, and CloudWatch Logs.",
        ),
        ConfigField(
            key="region",
            label="Region",
            kind="region",
            default="us-east-1",
            placeholder="us-east-1",
            help="Everything is created in this region. App Runner is not "
            "available in every region - check before choosing an unusual one.",
        ),
        ConfigField(
            key="codebuild_role_arn",
            label="CodeBuild service role ARN",
            placeholder="arn:aws:iam::123456789012:role/sutr-codebuild",
            help="An IAM role CodeBuild assumes to run the build. It must allow "
            "s3:GetObject on the source bucket, ecr:GetAuthorizationToken plus "
            "push permissions on the repository, and logs:CreateLogStream / "
            "logs:PutLogEvents. Create it once per account.",
        ),
        ConfigField(
            key="apprunner_access_role_arn",
            label="App Runner ECR access role ARN",
            placeholder="arn:aws:iam::123456789012:role/sutr-apprunner-ecr",
            help="An IAM role trusted by build.apprunner.amazonaws.com that "
            "grants read access to ECR - AWS publishes it as "
            "AWSAppRunnerServicePolicyForECRAccess. App Runner assumes it to "
            "pull the image.",
        ),
    )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _config(self, target: ProviderTarget) -> dict[str, str]:
        config = {
            "account": (target.config.get("account") or "").strip(),
            "role": (target.config.get("role") or "").strip(),
            "region": (target.config.get("region") or "us-east-1").strip(),
            "codebuild_role_arn": (target.config.get("codebuild_role_arn") or "").strip(),
            "apprunner_access_role_arn": (
                target.config.get("apprunner_access_role_arn") or ""
            ).strip(),
        }
        if not config["account"] or not config["role"]:
            raise ProviderError("Choose an AWS account and role for this deployment.")
        return config

    def _client(self, target: ProviderTarget) -> AwsClient:
        credentials = target.credentials
        if not credentials.get("access_key_id"):
            raise ProviderError(
                "No AWS credentials were issued for this deployment. The IAM "
                "Identity Center session may have expired - reconnect AWS."
            )
        return AwsClient(
            region=(target.config.get("region") or "us-east-1").strip(),
            access_key_id=credentials["access_key_id"],
            secret_access_key=credentials.get("secret_access_key", ""),
            session_token=credentials.get("session_token", ""),
        )

    async def available(self, target: ProviderTarget | None = None) -> tuple[bool, str | None]:
        if target is None:
            return True, None
        try:
            config = self._config(target)
        except ProviderError as exc:
            return False, str(exc)
        for key, label in (
            ("codebuild_role_arn", "CodeBuild service role ARN"),
            ("apprunner_access_role_arn", "App Runner ECR access role ARN"),
        ):
            if not config[key]:
                return False, f"The {label} is required."
            if not config[key].startswith("arn:aws"):
                return False, f"The {label} does not look like an IAM role ARN."
        try:
            client = self._client(target)
            await client.call("apprunner", "ListServices", {"MaxResults": 1})
        except ProviderError as exc:
            return False, str(exc)
        return True, None

    # ── deploy ───────────────────────────────────────────────────────────────

    async def deploy(self, spec: DeploySpec) -> dict:
        config = self._config(spec.target)
        ok, reason = await self.available(spec.target)
        if not ok:
            raise ProviderError(reason or "The AWS target is not usable.")

        client = self._client(spec.target)
        region = config["region"]
        account = config["account"]
        short = str(spec.deployment_id)[:8]
        name = f"{_dns_name(spec.slug)}-{short}"
        repository = _dns_name(spec.slug)
        registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
        image_uri = f"{registry}/{repository}:{spec.artifact_tag}"
        bucket = f"sutr-mcp-{account}-{region}"
        key = f"packages/{name}.zip"

        await self._ensure_bucket(client, bucket)
        await client.s3_put_object(bucket, key, spec.package_zip)
        await self._ensure_repository(client, repository)
        project = await self._ensure_project(client, config, name, bucket, key, registry, image_uri)
        await self._run_build(client, project)
        service = await self._create_service(client, config, spec, name, image_uri)

        service_url = service.get("ServiceUrl", "")
        return {
            "account": account,
            "region": region,
            "repository": repository,
            "bucket": bucket,
            "object": key,
            "project": project,
            "image": image_uri,
            "service_arn": service.get("ServiceArn", ""),
            "service_name": name,
            "url": f"https://{service_url}/mcp",
            "health_url": f"https://{service_url}/health",
            "console_url": (
                f"https://{region}.console.aws.amazon.com/apprunner/home?region={region}#/services"
            ),
        }

    async def _ensure_bucket(self, client: AwsClient, bucket: str) -> None:
        if await client.s3_head_bucket(bucket):
            return
        await client.s3_create_bucket(bucket)

    async def _ensure_repository(self, client: AwsClient, repository: str) -> None:
        try:
            await client.call("ecr", "CreateRepository", {"repositoryName": repository})
        except AwsError as exc:
            if exc.code != "RepositoryAlreadyExistsException":
                raise

    async def _ensure_project(
        self,
        client: AwsClient,
        config: dict[str, str],
        name: str,
        bucket: str,
        key: str,
        registry: str,
        image_uri: str,
    ) -> str:
        definition: dict[str, Any] = {
            "name": name,
            "source": {
                "type": "S3",
                "location": f"{bucket}/{key}",
                "buildspec": BUILDSPEC,
            },
            "artifacts": {"type": "NO_ARTIFACTS"},
            "environment": {
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL",
                # Building a container image inside a container needs it.
                "privilegedMode": True,
                "environmentVariables": [
                    {"name": "SUTR_REGISTRY", "value": registry},
                    {"name": "SUTR_IMAGE_URI", "value": image_uri},
                ],
            },
            "serviceRole": config["codebuild_role_arn"],
            "timeoutInMinutes": 20,
        }
        try:
            await client.call("codebuild", "CreateProject", definition)
        except AwsError as exc:
            if exc.code != "ResourceAlreadyExistsException":
                raise
            await client.call("codebuild", "UpdateProject", definition)
        return name

    async def _run_build(self, client: AwsClient, project: str) -> None:
        started = await client.call("codebuild", "StartBuild", {"projectName": project})
        build_id = (started.get("build") or {}).get("id")
        if not build_id:
            raise ProviderError("CodeBuild did not return a build id.")

        async def fetch() -> dict:
            payload = await client.call("codebuild", "BatchGetBuilds", {"ids": [build_id]})
            builds = payload.get("builds") or []
            return builds[0] if builds else {}

        result = await poll_until(
            fetch,
            lambda build: build.get("buildStatus") in _TERMINAL_BUILD,
            provider=PROVIDER,
            what="building the container image",
            timeout_seconds=BUILD_TIMEOUT_SECONDS,
        )
        if result.get("buildStatus") != "SUCCEEDED":
            phase = result.get("currentPhase") or "unknown phase"
            raise ProviderError(
                f"CodeBuild finished with status {result.get('buildStatus')} during "
                f"{phase}. Open build {build_id} in the CodeBuild console for the log. "
                "A failure in pre_build usually means the service role cannot reach ECR."
            )

    async def _create_service(
        self,
        client: AwsClient,
        config: dict[str, str],
        spec: DeploySpec,
        name: str,
        image_uri: str,
    ) -> dict:
        created = await client.call(
            "apprunner",
            "CreateService",
            {
                "ServiceName": name,
                "SourceConfiguration": {
                    "ImageRepository": {
                        "ImageIdentifier": image_uri,
                        "ImageRepositoryType": "ECR",
                        "ImageConfiguration": {
                            "Port": str(spec.internal_port),
                            "RuntimeEnvironmentVariables": dict(spec.env),
                        },
                    },
                    "AuthenticationConfiguration": {
                        "AccessRoleArn": config["apprunner_access_role_arn"]
                    },
                    "AutoDeploymentsEnabled": False,
                },
                "InstanceConfiguration": {"Cpu": "1024", "Memory": "2048"},
                "HealthCheckConfiguration": {"Protocol": "HTTP", "Path": "/health"},
                "Tags": [{"Key": "sutr-deployment", "Value": str(spec.deployment_id)}],
            },
        )
        service = created.get("Service") or {}
        arn = service.get("ServiceArn")
        if not arn:
            raise ProviderError("App Runner did not return a service ARN.")

        final = await poll_until(
            lambda: self._describe(client, arn),
            lambda payload: payload.get("Status") in _TERMINAL_SERVICE,
            provider=PROVIDER,
            what="starting the App Runner service",
            timeout_seconds=SERVICE_TIMEOUT_SECONDS,
        )
        if final.get("Status") != "RUNNING":
            raise ProviderError(
                f"App Runner reported {final.get('Status')}. The most common cause "
                "is the ECR access role not being assumable by App Runner."
            )
        return final

    async def _describe(self, client: AwsClient, arn: str) -> dict:
        payload = await client.call("apprunner", "DescribeService", {"ServiceArn": arn})
        return payload.get("Service") or {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _arn(self, state: dict) -> str:
        arn = state.get("service_arn")
        if not arn:
            raise ProviderError("This deployment has no App Runner service recorded.")
        return arn

    async def metrics(self, state: dict, target: ProviderTarget) -> ProviderMetrics:
        """What App Runner's DescribeService reports.

        Request counts, latencies, CPU and memory utilisation live in
        CloudWatch, which needs its own permissions on the assumed role.
        Rather than inventing numbers, this reports the configured instance
        size and readiness, and names what is missing.
        """
        try:
            service = await self._describe(self._client(target), self._arn(state))
        except AwsError as exc:
            return ProviderMetrics(source="app runner", unavailable_reason=str(exc))
        configuration = service.get("InstanceConfiguration") or {}
        memory = configuration.get("Memory")
        try:
            memory_bytes = int(str(memory).rstrip("Gg").strip()) * 1024**3 if memory else None
        except ValueError:
            memory_bytes = None
        return ProviderMetrics(
            healthy=service.get("Status") == "RUNNING",
            memory_limit_bytes=memory_bytes,
            source="app runner DescribeService",
            unavailable_reason=(
                "Request counts, latencies and utilisation require CloudWatch "
                "(GetMetricData on AWS/AppRunner), which the deploy role does not grant."
            ),
        )

    async def status(self, state: dict, target: ProviderTarget) -> ProviderStatus:
        client = self._client(target)
        try:
            service = await self._describe(client, self._arn(state))
        except AwsError as exc:
            if exc.code in ("ResourceNotFoundException", "InvalidRequestException"):
                return ProviderStatus(state="not_found", detail="The service no longer exists.")
            raise
        status = service.get("Status")
        if status == "RUNNING":
            return ProviderStatus(state="running")
        if status in ("PAUSED", "PAUSE_IN_PROGRESS"):
            return ProviderStatus(state="stopped", detail="The service is paused.")
        if status in ("DELETED", "DELETE_IN_PROGRESS"):
            return ProviderStatus(state="not_found", detail="The service is being deleted.")
        if status in ("CREATE_FAILED", "OPERATION_IN_PROGRESS"):
            failed = status == "CREATE_FAILED"
            return ProviderStatus(
                state="error" if failed else "running",
                detail=None if not failed else "App Runner could not start the service.",
            )
        return ProviderStatus(state="error", detail=f"Unexpected status {status}.")

    async def start(self, state: dict, target: ProviderTarget) -> None:
        await self._client(target).call(
            "apprunner", "ResumeService", {"ServiceArn": self._arn(state)}
        )

    async def stop(self, state: dict, target: ProviderTarget) -> None:
        await self._client(target).call(
            "apprunner", "PauseService", {"ServiceArn": self._arn(state)}
        )

    async def remove(self, state: dict, target: ProviderTarget) -> None:
        client = self._client(target)
        arn = state.get("service_arn")
        if arn:
            try:
                await client.call("apprunner", "DeleteService", {"ServiceArn": arn})
            except AwsError as exc:
                if exc.code not in ("ResourceNotFoundException", "InvalidStateException"):
                    raise
        # The CodeBuild project and the staged object exist only for this
        # deployment, so both go. The bucket and the ECR repository are shared
        # and stay.
        project = state.get("project")
        if project:
            try:
                await client.call("codebuild", "DeleteProject", {"name": project})
            except AwsError:
                pass
        bucket, key = state.get("bucket"), state.get("object")
        if bucket and key:
            try:
                await client.s3_delete_object(bucket, key)
            except ProviderError:
                pass

    async def logs(self, state: dict, target: ProviderTarget, tail: int = 100) -> str:
        arn = state.get("service_arn")
        name = state.get("service_name")
        if not arn or not name:
            return ""
        # App Runner names the log group after the service and the service id,
        # which is the ARN's final segment.
        service_id = arn.rsplit("/", 1)[-1]
        group = f"/aws/apprunner/{name}/{service_id}/application"
        client = self._client(target)
        try:
            payload = await client.call(
                "logs",
                "FilterLogEvents",
                {"logGroupName": group, "limit": min(tail, 1000)},
            )
        except AwsError as exc:
            if exc.code == "ResourceNotFoundException":
                return (
                    f"No log group {group} yet. App Runner creates it on the "
                    "service's first request."
                )
            raise
        events = payload.get("events") or []
        return "\n".join(
            f"{event.get('timestamp', '')} {event.get('message', '').rstrip()}" for event in events
        )


def _dns_name(slug: str) -> str:
    """App Runner service and ECR repository names: lowercase, hyphens."""
    cleaned = "".join(c if c.isalnum() else "-" for c in slug.lower()).strip("-")
    return (cleaned or "mcp-server")[:30].strip("-")


__all__ = ["AwsProvider", "BUILDSPEC"]
