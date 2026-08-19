"""DockerProvider unit tests: exact CLI invocations and output parsing,
with the docker binary faked out."""

import uuid

import pytest

from sutr.deploy.base import DeploySpec, ProviderError
from sutr.deploy.docker_provider import DockerProvider
from sutr.openapi.packaging import build_server_package
from tests.test_openapi.test_packaging import TOOLS


def _package() -> bytes:
    return build_server_package(
        name="Petstore Runner",
        base_url="https://api.petstore.example.com/v1",
        token_header="X-Api-Key",
        token_format="{token}",
        tools=TOOLS,
        api_title="Petstore",
        api_version="1.2.0",
    )[1]


class FakeCli:
    """Replaces DockerProvider._run; records commands, returns canned output."""

    def __init__(self):
        self.commands: list[tuple[str, ...]] = []
        self.outputs = {
            "build": "",
            "rm": "",
            "run": "abc123containerid",
            "port": "127.0.0.1:49523",
            "start": "",
            "stop": "",
            "rmi": "",
        }
        self.errors: dict[str, str] = {}

    async def __call__(self, *args: str, timeout: int = 60) -> str:
        self.commands.append(args)
        if args[0] in self.errors:
            raise ProviderError(self.errors[args[0]])
        return self.outputs.get(args[0], "")


@pytest.fixture(name="provider")
def provider_fixture(monkeypatch):
    provider = DockerProvider()
    fake = FakeCli()
    monkeypatch.setattr(provider, "_run", fake)
    provider._fake = fake
    return provider


async def test_deploy_builds_and_runs_with_expected_flags(provider):
    deployment_id = uuid.uuid4()
    spec = DeploySpec(
        deployment_id=deployment_id,
        name="Petstore Runner",
        slug="petstore_runner",
        package_zip=_package(),
        env={"PETSTORE_RUNNER_API_TOKEN": "sk_secret"},
    )
    state = await provider.deploy(spec)

    fake = provider._fake
    build = next(c for c in fake.commands if c[0] == "build")
    short = str(deployment_id)[:8]
    assert build[2] == f"sutr-deploy-petstore_runner:{short}"

    run = next(c for c in fake.commands if c[0] == "run")
    joined = " ".join(run)
    assert "--name sutr-petstore_runner-" in joined
    assert f"--label sutr.deployment={deployment_id}" in joined
    assert "-p 127.0.0.1:0:8000" in joined  # loopback-only, ephemeral port
    assert "-e PETSTORE_RUNNER_API_TOKEN=sk_secret" in joined
    assert "--restart unless-stopped" in joined
    assert "--memory 256m" in joined
    assert joined.endswith("--transport http --host 0.0.0.0 --port 8000")

    assert state["container_id"] == "abc123containerid"
    assert state["host_port"] == "49523"
    assert state["url"] == "http://127.0.0.1:49523/mcp"
    assert state["health_url"] == "http://127.0.0.1:49523/health"


async def test_status_parsing(provider):
    fake = provider._fake
    state = {"container_name": "sutr-x-abc"}

    fake.outputs["inspect"] = "running|"
    assert (await provider.status(state)).state == "running"

    fake.outputs["inspect"] = "exited|oom killed"
    result = await provider.status(state)
    assert result.state == "stopped"
    assert "oom killed" in result.detail

    fake.errors["inspect"] = "docker inspect failed: No such object: sutr-x-abc"
    assert (await provider.status(state)).state == "not_found"

    assert (await provider.status({})).state == "not_found"


async def test_remove_is_idempotent_and_cleans_image(provider):
    fake = provider._fake
    fake.errors["rm"] = "no such container"
    fake.errors["rmi"] = "no such image"
    # Errors on both are swallowed — remove never raises.
    await provider.remove({"container_name": "gone", "image_tag": "gone:1"})
    assert ("rm", "-f", "gone") in fake.commands
    assert ("rmi", "gone:1") in fake.commands


async def test_available_reports_daemon_errors(provider):
    provider._fake.errors["version"] = "docker version failed: cannot connect to the daemon"
    ok, reason = await provider.available()
    assert not ok
    assert "daemon" in reason
