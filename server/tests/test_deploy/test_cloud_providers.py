"""The three cloud deployment providers, against recorded API shapes.

These exercise the orchestration — call order, what is created, what the
returned state contains, how a failed build is reported — with the cloud APIs
mocked at the HTTP layer. They cannot prove the request bodies are what a real
Cloud Build or ACR Task accepts; only a live account can, and that is stated
plainly in the progress notes rather than implied by a green test.

What they do prove is the part most likely to rot: that a credential for one
cloud never travels to another, that "already exists" is not an error, and
that a failed build surfaces as a failure rather than a running deployment.
"""

import io
import json
import uuid
import zipfile

import httpx
import pytest

from sutr.deploy.aws.provider import AwsProvider
from sutr.deploy.azure_provider import AzureProvider
from sutr.deploy.base import DeploySpec, ProviderError, ProviderTarget
from sutr.deploy.cloud_http import zip_to_tar_gz
from sutr.deploy.gcp_provider import GcpProvider


def _package() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Dockerfile", "FROM python:3.12-slim\n")
        archive.writestr("server.py", "print('hi')\n")
    return buffer.getvalue()


def _spec(target: ProviderTarget, slug: str = "petstore") -> DeploySpec:
    return DeploySpec(
        deployment_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        name="Petstore",
        slug=slug,
        package_zip=_package(),
        env={"PETSTORE_API_TOKEN": "sk_live"},
        target=target,
    )


class Recorder:
    """Records every request and answers from a routing table."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        self.bodies.append(request.content)
        for match, response in self.routes:
            if match in url:
                return response(request) if callable(response) else response
        return httpx.Response(200, json={})

    def hit(self, fragment: str) -> bool:
        return any(fragment in url for _method, url in self.calls)

    def body_for(self, fragment: str) -> dict:
        for (_method, url), body in zip(self.calls, self.bodies):
            if fragment in url and body:
                return json.loads(body)
        raise AssertionError(f"no JSON body sent to a URL containing {fragment!r}")


@pytest.fixture(name="patch_http")
def patch_http_fixture(monkeypatch):
    def apply(recorder, *modules):
        class Patched(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(recorder)
                super().__init__(*args, **kwargs)

        for module in modules:
            monkeypatch.setattr(f"{module}.httpx.AsyncClient", Patched)

    return apply


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Poll loops are correct but slow; the tests care about the sequence."""

    async def instant(_seconds):
        return None

    monkeypatch.setattr("sutr.deploy.cloud_http.asyncio.sleep", instant)


# ── Google Cloud ─────────────────────────────────────────────────────────────

GCP_TARGET = ProviderTarget(
    config={"project": "acme-prod", "region": "us-central1", "repository": "sutr"},
    credentials={"access_token": "ya29.gcp"},
)


def _gcp_routes(build_status="SUCCESS", repo_exists=True):
    return [
        (
            "artifactregistry.googleapis.com",
            # The existence check is the GET; the create is the POST, and a
            # create must not inherit the 404 that triggered it.
            lambda request: httpx.Response(
                200 if (repo_exists or request.method != "GET") else 404,
                json={"name": "repo", "done": True},
            ),
        ),
        ("storage.googleapis.com/upload", httpx.Response(200, json={})),
        ("storage.googleapis.com/storage/v1/b/acme-prod_cloudbuild", httpx.Response(200, json={})),
        (
            "cloudbuild.googleapis.com/v1/projects/acme-prod/locations/us-central1/builds/b1",
            httpx.Response(200, json={"status": build_status, "logUrl": "https://logs"}),
        ),
        (
            "cloudbuild.googleapis.com",
            httpx.Response(200, json={"metadata": {"build": {"id": "b1"}}}),
        ),
        (
            "run.googleapis.com",
            lambda request: httpx.Response(
                200,
                json={
                    "uri": "https://petstore-12345678-uc.a.run.app",
                    "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
                    "done": True,
                },
            ),
        ),
    ]


async def test_gcp_builds_then_deploys_and_publishes_the_mcp_url(patch_http):
    recorder = Recorder(_gcp_routes())
    patch_http(recorder, "sutr.deploy.cloud_http")

    state = await GcpProvider().deploy(_spec(GCP_TARGET))

    assert state["url"] == "https://petstore-12345678-uc.a.run.app/mcp"
    assert state["health_url"].endswith("/health")
    # The tag carries the revision: the previous revision's image stays in the
    # registry, which is what makes a rollback a re-run rather than a rebuild.
    assert state["image"] == "us-central1-docker.pkg.dev/acme-prod/sutr/petstore:12345678-r1"
    assert recorder.hit("storage.googleapis.com/upload")
    assert recorder.hit("cloudbuild.googleapis.com")
    assert recorder.hit(":setIamPolicy")


async def test_gcp_passes_the_http_transport_args_and_the_runtime_env(patch_http):
    """The image entrypoint defaults to MCP stdio; without these args the
    container starts and listens to nobody."""
    recorder = Recorder(_gcp_routes())
    patch_http(recorder, "sutr.deploy.cloud_http")
    await GcpProvider().deploy(_spec(GCP_TARGET))

    body = recorder.body_for(
        "run.googleapis.com/v2/projects/acme-prod/locations/us-central1/services"
    )
    container = body["template"]["containers"][0]
    assert container["args"] == ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
    assert {"name": "PETSTORE_API_TOKEN", "value": "sk_live"} in container["env"]


async def test_gcp_creates_the_registry_only_when_missing(patch_http):
    recorder = Recorder(_gcp_routes(repo_exists=False))
    patch_http(recorder, "sutr.deploy.cloud_http")
    await GcpProvider().deploy(_spec(GCP_TARGET))
    assert any(method == "POST" and "repositories" in url for method, url in recorder.calls)


async def test_gcp_reports_a_failed_build_with_the_log_url(patch_http):
    recorder = Recorder(_gcp_routes(build_status="FAILURE"))
    patch_http(recorder, "sutr.deploy.cloud_http")
    with pytest.raises(ProviderError) as excinfo:
        await GcpProvider().deploy(_spec(GCP_TARGET))
    assert "FAILURE" in str(excinfo.value)
    assert "https://logs" in str(excinfo.value)
    # Nothing was deployed, so no service was ever asked for.
    assert not recorder.hit("run.googleapis.com")


async def test_gcp_explains_a_blocked_public_invoker_binding(patch_http):
    routes = _gcp_routes()
    routes.insert(0, (":setIamPolicy", httpx.Response(403, json={"error": {"message": "denied"}})))
    recorder = Recorder(routes)
    patch_http(recorder, "sutr.deploy.cloud_http")
    with pytest.raises(ProviderError) as excinfo:
        await GcpProvider().deploy(_spec(GCP_TARGET))
    assert "Domain restricted sharing" in str(excinfo.value)


async def test_gcp_stop_makes_the_service_unreachable_without_destroying_it(patch_http):
    recorder = Recorder([("run.googleapis.com", httpx.Response(200, json={}))])
    patch_http(recorder, "sutr.deploy.cloud_http")
    state = {"project": "acme-prod", "region": "us-central1", "service": "petstore-12345678"}
    await GcpProvider().stop(state, GCP_TARGET)
    assert recorder.body_for("services/petstore-12345678") == {
        "ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY"
    }
    assert not any(method == "DELETE" for method, _url in recorder.calls)


async def test_gcp_status_maps_internal_ingress_to_stopped(patch_http):
    recorder = Recorder(
        [
            (
                "run.googleapis.com",
                httpx.Response(200, json={"ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY"}),
            )
        ]
    )
    patch_http(recorder, "sutr.deploy.cloud_http")
    state = {"project": "p", "region": "r", "service": "s"}
    assert (await GcpProvider().status(state, GCP_TARGET)).state == "stopped"


async def test_gcp_status_maps_a_missing_service_to_not_found(patch_http):
    recorder = Recorder([("run.googleapis.com", httpx.Response(404, json={}))])
    patch_http(recorder, "sutr.deploy.cloud_http")
    state = {"project": "p", "region": "r", "service": "s"}
    assert (await GcpProvider().status(state, GCP_TARGET)).state == "not_found"


async def test_gcp_refuses_to_deploy_without_a_project():
    with pytest.raises(ProviderError) as excinfo:
        await GcpProvider().deploy(_spec(ProviderTarget(credentials={"access_token": "t"})))
    assert "project" in str(excinfo.value)


# ── Azure ────────────────────────────────────────────────────────────────────

AZURE_TARGET = ProviderTarget(
    config={
        "subscription": "sub-1",
        "location": "eastus",
        "resource_group": "sutr-mcp",
        "registry": "acmemcp",
        "environment": "sutr-mcp-env",
    },
    credentials={"access_token": "azure-arm-token"},
)


def _azure_routes(run_status="Succeeded"):
    return [
        (
            "listBuildSourceUploadUrl",
            httpx.Response(
                200,
                json={
                    "uploadUrl": "https://acmemcp.blob.core.windows.net/src?sig=abc",
                    "relativePath": "source/upload.tar.gz",
                },
            ),
        ),
        ("blob.core.windows.net", httpx.Response(201)),
        ("scheduleRun", httpx.Response(200, json={"name": "ca1"})),
        (
            "/runs/ca1",
            httpx.Response(200, json={"properties": {"status": run_status}}),
        ),
        (
            "listCredentials",
            httpx.Response(
                200, json={"username": "acmemcp", "passwords": [{"value": "registry-pw"}]}
            ),
        ),
        (
            "managedEnvironments/sutr-mcp-env",
            httpx.Response(
                200,
                json={
                    "id": "/subscriptions/sub-1/../managedEnvironments/sutr-mcp-env",
                    "properties": {"provisioningState": "Succeeded"},
                },
            ),
        ),
        (
            "containerApps/",
            httpx.Response(
                200,
                json={
                    "properties": {
                        "provisioningState": "Succeeded",
                        "configuration": {
                            "ingress": {"fqdn": "petstore.eastus.azurecontainerapps.io"}
                        },
                    }
                },
            ),
        ),
        (
            "registries/acmemcp",
            httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}}),
        ),
        (
            "management.azure.com",
            httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}}),
        ),
    ]


async def test_azure_builds_in_the_registry_then_runs_a_container_app(patch_http):
    recorder = Recorder(_azure_routes())
    patch_http(recorder, "sutr.deploy.cloud_http")

    state = await AzureProvider().deploy(_spec(AZURE_TARGET))

    assert state["url"] == "https://petstore.eastus.azurecontainerapps.io/mcp"
    assert state["image"] == "acmemcp.azurecr.io/petstore:12345678-r1"
    assert recorder.hit("listBuildSourceUploadUrl")
    assert recorder.hit("scheduleRun")
    assert recorder.hit("containerApps/petstore-12345678")


async def test_azure_uploads_a_tar_not_the_zip(patch_http):
    """ACR builds accept a tar.gz source archive and nothing else."""
    recorder = Recorder(_azure_routes())
    patch_http(recorder, "sutr.deploy.cloud_http")
    await AzureProvider().deploy(_spec(AZURE_TARGET))

    for (_method, url), body in zip(recorder.calls, recorder.bodies):
        if "blob.core.windows.net" in url:
            assert body.startswith(b"\x1f\x8b")  # gzip magic
            break
    else:
        raise AssertionError("the source archive was never uploaded")


async def test_azure_sends_no_bearer_token_to_the_sas_upload_url(patch_http):
    """A SAS URL carries its own authorization; a bearer token makes Azure
    Storage reject it, and would leak an ARM token to a storage endpoint."""
    seen = {}

    def handler(request):
        if "blob.core.windows.net" in str(request.url):
            seen["auth"] = request.headers.get("authorization")
        for match, response in _azure_routes():
            if match in str(request.url):
                return response
        return httpx.Response(200, json={})

    patch_http(handler, "sutr.deploy.cloud_http")
    await AzureProvider().deploy(_spec(AZURE_TARGET))
    assert seen["auth"] is None


async def test_azure_puts_runtime_secrets_in_the_secret_store_not_the_template(patch_http):
    recorder = Recorder(_azure_routes())
    patch_http(recorder, "sutr.deploy.cloud_http")
    await AzureProvider().deploy(_spec(AZURE_TARGET))

    body = recorder.body_for("containerApps/petstore-12345678")
    container = body["properties"]["template"]["containers"][0]
    env = container["env"][0]
    assert env["name"] == "PETSTORE_API_TOKEN"
    assert "value" not in env and env["secretRef"]
    secrets = {
        entry["name"]: entry["value"] for entry in body["properties"]["configuration"]["secrets"]
    }
    assert secrets[env["secretRef"]] == "sk_live"
    assert secrets["sutr-registry-password"] == "registry-pw"


async def test_azure_reports_a_failed_image_build(patch_http):
    recorder = Recorder(_azure_routes(run_status="Failed"))
    patch_http(recorder, "sutr.deploy.cloud_http")
    with pytest.raises(ProviderError) as excinfo:
        await AzureProvider().deploy(_spec(AZURE_TARGET))
    assert "Failed" in str(excinfo.value)
    assert not recorder.hit("containerApps/petstore")


@pytest.mark.parametrize("registry", ["", "ab", "acme-mcp", "acme_mcp"])
async def test_azure_rejects_an_invalid_registry_name_before_creating_anything(registry):
    target = ProviderTarget(
        config={**AZURE_TARGET.config, "registry": registry},
        credentials=AZURE_TARGET.credentials,
    )
    ok, reason = await AzureProvider().available(target)
    assert ok is False
    assert "registry" in reason.lower()


async def test_azure_remove_deletes_only_the_app(patch_http):
    """The resource group, registry, and environment are shared across
    deployments; tearing them down with one would break the others."""
    recorder = Recorder([("management.azure.com", httpx.Response(200, json={}))])
    patch_http(recorder, "sutr.deploy.cloud_http")
    await AzureProvider().remove(
        {"subscription": "sub-1", "resource_group": "sutr-mcp", "app": "petstore-12345678"},
        AZURE_TARGET,
    )
    deletes = [url for method, url in recorder.calls if method == "DELETE"]
    assert len(deletes) == 1 and "containerApps/petstore-12345678" in deletes[0]


async def test_azure_logs_point_at_the_portal_rather_than_returning_nothing(patch_http):
    """Container Apps keeps logs outside ARM; an empty string would read as
    'the server printed nothing'."""
    output = await AzureProvider().logs(
        {"console_url": "https://portal.azure.com/#resource/x"}, AZURE_TARGET
    )
    assert "Log Analytics" in output
    assert "https://portal.azure.com/#resource/x" in output


# ── AWS ──────────────────────────────────────────────────────────────────────

AWS_TARGET = ProviderTarget(
    config={
        "account": "111122223333",
        "role": "AdministratorAccess",
        "region": "us-east-1",
        "codebuild_role_arn": "arn:aws:iam::111122223333:role/sutr-codebuild",
        "apprunner_access_role_arn": "arn:aws:iam::111122223333:role/sutr-apprunner-ecr",
    },
    credentials={
        "access_key_id": "ASIAEXAMPLE",
        "secret_access_key": "secret",
        "session_token": "session",
    },
)


SERVICE_ARN = "arn:aws:apprunner:us-east-1:111122223333:service/petstore/abc123"


def _aws_handler(build_status="SUCCEEDED", service_status="RUNNING"):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        target = request.headers.get("x-amz-target", "")
        calls.append(target or f"{request.method} {request.url.path}")
        if target.endswith("CreateRepository"):
            return httpx.Response(200, json={})
        if target.endswith("CreateProject") or target.endswith("UpdateProject"):
            return httpx.Response(200, json={"project": {"name": "petstore-12345678"}})
        if target.endswith("StartBuild"):
            return httpx.Response(200, json={"build": {"id": "build-1"}})
        if target.endswith("BatchGetBuilds"):
            return httpx.Response(
                200, json={"builds": [{"buildStatus": build_status, "currentPhase": "BUILD"}]}
            )
        if target.endswith("CreateService"):
            return httpx.Response(
                200,
                json={
                    "Service": {
                        "ServiceArn": SERVICE_ARN,
                        "ServiceUrl": "abc123.us-east-1.awsapprunner.com",
                        "Status": "OPERATION_IN_PROGRESS",
                    }
                },
            )
        if target.endswith("DescribeService"):
            return httpx.Response(
                200,
                json={
                    "Service": {
                        "ServiceArn": SERVICE_ARN,
                        "ServiceUrl": "abc123.us-east-1.awsapprunner.com",
                        "Status": service_status,
                    }
                },
            )
        if target.endswith("ListServices"):
            return httpx.Response(200, json={"ServiceSummaryList": []})
        # S3: HEAD/PUT on the bucket and object.
        return httpx.Response(200)

    handler.calls = calls
    return handler


async def test_aws_stages_the_package_builds_it_and_starts_app_runner(patch_http, monkeypatch):
    handler = _aws_handler()

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.deploy.aws.client.httpx.AsyncClient", Patched)

    state = await AwsProvider().deploy(_spec(AWS_TARGET))

    assert state["url"] == "https://abc123.us-east-1.awsapprunner.com/mcp"
    assert state["image"] == "111122223333.dkr.ecr.us-east-1.amazonaws.com/petstore:12345678-r1"
    assert state["bucket"] == "sutr-mcp-111122223333-us-east-1"
    ordered = [call for call in handler.calls if "." in call]
    assert "AmazonEC2ContainerRegistry_V20150921.CreateRepository" in ordered
    assert ordered.index("CodeBuild_20161006.StartBuild") < ordered.index("AppRunner.CreateService")


async def test_aws_reports_a_failed_build_and_never_creates_the_service(patch_http, monkeypatch):
    handler = _aws_handler(build_status="FAILED")

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.deploy.aws.client.httpx.AsyncClient", Patched)

    with pytest.raises(ProviderError) as excinfo:
        await AwsProvider().deploy(_spec(AWS_TARGET))
    assert "FAILED" in str(excinfo.value)
    assert "AppRunner.CreateService" not in handler.calls


async def test_aws_reports_a_service_that_could_not_start(patch_http, monkeypatch):
    handler = _aws_handler(service_status="CREATE_FAILED")

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.deploy.aws.client.httpx.AsyncClient", Patched)
    with pytest.raises(ProviderError) as excinfo:
        await AwsProvider().deploy(_spec(AWS_TARGET))
    assert "ECR access role" in str(excinfo.value)


@pytest.mark.parametrize("missing", ["codebuild_role_arn", "apprunner_access_role_arn"])
async def test_aws_requires_both_iam_roles_up_front(missing):
    config = {**AWS_TARGET.config, missing: ""}
    ok, reason = await AwsProvider().available(
        ProviderTarget(config=config, credentials=AWS_TARGET.credentials)
    )
    assert ok is False
    assert "ARN" in reason


async def test_aws_rejects_a_value_that_is_not_an_arn():
    config = {**AWS_TARGET.config, "codebuild_role_arn": "sutr-codebuild"}
    ok, reason = await AwsProvider().available(
        ProviderTarget(config=config, credentials=AWS_TARGET.credentials)
    )
    assert ok is False
    assert "does not look like an IAM role ARN" in reason


async def test_aws_without_credentials_says_the_session_expired():
    with pytest.raises(ProviderError) as excinfo:
        await AwsProvider().deploy(_spec(ProviderTarget(config=AWS_TARGET.config)))
    assert "Identity Center session" in str(excinfo.value)


async def test_the_buildspec_overrides_the_image_command_to_serve_http():
    """`python server.py` defaults to stdio; the second build re-tags the image
    with an explicit CMD so App Runner's StartCommand semantics never matter."""
    from sutr.deploy.aws.provider import BUILDSPEC

    assert "--transport" in BUILDSPEC and '"http"' in BUILDSPEC
    assert BUILDSPEC.count("docker build") == 2
    assert "docker push" in BUILDSPEC


# ── shared plumbing ──────────────────────────────────────────────────────────


def test_zip_to_tar_gz_preserves_every_file():
    tar_gz = zip_to_tar_gz(_package())
    import gzip
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tar_gz))) as tar:
        assert sorted(tar.getnames()) == ["Dockerfile", "server.py"]


def test_zip_to_tar_gz_is_deterministic():
    """The zip generator is deliberately reproducible; the tar must not undo it."""
    package = _package()
    assert zip_to_tar_gz(package) == zip_to_tar_gz(package)
