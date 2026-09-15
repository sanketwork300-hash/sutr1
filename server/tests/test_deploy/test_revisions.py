"""Deployment update, rollback, history, and metrics (build prompt §36/§37, ADR-016).

The rule being enforced: an update is a **new revision of the same
deployment**, never a delete followed by a create. The deployment keeps its
identity and its URL, every version's artifact is retained, and a rollback
re-runs a retained artifact rather than rebuilding from source that may since
have changed.
"""

import json
import uuid

import pytest

from sutr.deploy.base import DeploymentProvider, DeploySpec, ProviderMetrics, ProviderTarget
from sutr.deploy.docker_provider import DockerProvider, _parse_stats
from sutr.deploy.swaraj_provider import BLOCKED_CODE, SwarajCloudProvider
from sutr.models.deployment import Deployment
from sutr.models.deployment_revision import (
    ORIGIN_CREATE,
    ORIGIN_ROLLBACK,
    ORIGIN_UPDATE,
    OUTCOME_ACTIVE,
    OUTCOME_FAILED,
    OUTCOME_SUPERSEDED,
)
from sutr.services import deployments as service
from tests.test_deploy.test_docker_provider import FakeCli

# ── The artifact tag is what makes a rollback a re-run ───────────────────────


def test_the_artifact_tag_carries_the_revision():
    spec = DeploySpec(
        deployment_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        name="n",
        slug="s",
        package_zip=b"",
        revision=7,
    )
    assert spec.artifact_tag == "12345678-r7"


def test_two_revisions_of_one_deployment_produce_different_artifacts():
    base = dict(
        deployment_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        name="n",
        slug="s",
        package_zip=b"",
    )
    assert (
        DeploySpec(**base, revision=1).artifact_tag != DeploySpec(**base, revision=2).artifact_tag
    )


# ── Docker: an update keeps the port, and therefore the URL ──────────────────


async def test_an_update_reuses_the_published_port_so_the_url_survives(monkeypatch):
    provider = DockerProvider()
    fake = FakeCli()
    monkeypatch.setattr(provider, "_run", fake)

    spec = DeploySpec(
        deployment_id=uuid.uuid4(),
        name="Petstore",
        slug="petstore",
        package_zip=_minimal_zip(),
        revision=2,
        previous_state={"host_port": "49523"},
    )
    state = await provider.update(spec)
    run = next(c for c in fake.commands if c[0] == "run")
    assert "127.0.0.1:49523:8000" in run
    assert state["url"] == "http://127.0.0.1:49523/mcp"
    assert state["revision"] == 2


async def test_a_first_deploy_asks_for_any_free_port(monkeypatch):
    provider = DockerProvider()
    fake = FakeCli()
    monkeypatch.setattr(provider, "_run", fake)
    await provider.deploy(
        DeploySpec(
            deployment_id=uuid.uuid4(), name="P", slug="p", package_zip=_minimal_zip(), revision=1
        )
    )
    run = next(c for c in fake.commands if c[0] == "run")
    assert "127.0.0.1:0:8000" in run


def _minimal_zip() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Dockerfile", "FROM scratch\n")
    return buffer.getvalue()


# ── Docker metrics ───────────────────────────────────────────────────────────


async def test_docker_reports_cpu_and_memory(monkeypatch):
    provider = DockerProvider()
    fake = FakeCli()
    fake.outputs["stats"] = "12.34%|45.5MiB / 256MiB"
    fake.outputs["inspect"] = "true"
    monkeypatch.setattr(provider, "_run", fake)

    metrics = await provider.metrics({"container_name": "sutr-p-1"}, ProviderTarget())
    assert metrics.cpu_percent == 12.34
    assert metrics.memory_bytes == 47710208
    assert metrics.memory_limit_bytes == 268435456
    assert metrics.healthy is True
    assert metrics.replicas == 1
    assert metrics.unavailable_reason is None


async def test_docker_metrics_explain_themselves_when_unavailable(monkeypatch):
    provider = DockerProvider()
    fake = FakeCli()
    fake.errors["stats"] = "docker stats failed: no such container"
    monkeypatch.setattr(provider, "_run", fake)
    metrics = await provider.metrics({"container_name": "gone"}, ProviderTarget())
    assert metrics.cpu_percent is None
    assert "no such container" in metrics.unavailable_reason


def test_unparseable_stats_yield_nothing_rather_than_a_wrong_number():
    assert _parse_stats("--|-- / --") == (None, None, None)


# ── A provider that cannot update says so ────────────────────────────────────


class _NoUpdateProvider(DeploymentProvider):
    id = "no-update"
    display_name = "Immutable Cloud"
    supports_update = False

    async def available(self, target=None):
        return True, None

    async def deploy(self, spec):
        return {"url": "https://x.example.com/mcp"}

    async def status(self, state, target): ...
    async def start(self, state, target): ...
    async def stop(self, state, target): ...
    async def remove(self, state, target): ...
    async def logs(self, state, target, tail=100):
        return ""


async def test_a_provider_that_cannot_update_refuses_rather_than_recreating():
    from sutr.deploy.base import ProviderError

    provider = _NoUpdateProvider()
    with pytest.raises(ProviderError, match="cannot update"):
        await provider.update(
            DeploySpec(deployment_id=uuid.uuid4(), name="n", slug="s", package_zip=b"")
        )


async def test_the_default_metrics_answer_names_the_provider_rather_than_returning_zeros():
    metrics = await _NoUpdateProvider().metrics({}, ProviderTarget())
    assert metrics.cpu_percent is None
    assert "does not report runtime metrics" in metrics.unavailable_reason
    assert metrics.as_dict()["memory_bytes"] is None


# ── Swaraj Cloud: blocked, and honest about it (ADR-006) ─────────────────────


async def test_swaraj_reports_documentation_required_rather_than_pretending():
    provider = SwarajCloudProvider()
    usable, reason = await provider.available()
    assert usable is False
    assert BLOCKED_CODE in reason
    assert "documentation" in reason.lower()


@pytest.mark.parametrize("operation", ["deploy", "update", "rollback", "start", "stop"])
async def test_every_swaraj_operation_refuses_loudly(operation):
    from sutr.deploy.base import ProviderError

    provider = SwarajCloudProvider()
    spec = DeploySpec(deployment_id=uuid.uuid4(), name="n", slug="s", package_zip=b"")
    argument = spec if operation in ("deploy", "update", "rollback") else {}
    with pytest.raises(ProviderError, match="DOCUMENTATION_REQUIRED"):
        if operation in ("deploy", "update", "rollback"):
            await getattr(provider, operation)(argument)
        else:
            await getattr(provider, operation)(argument, ProviderTarget())


async def test_swaraj_remove_is_idempotent_because_nothing_was_ever_created():
    assert await SwarajCloudProvider().remove({}, ProviderTarget()) is None


def test_swaraj_is_registered_and_visibly_disabled():
    from sutr.deploy.registry import get_provider, list_providers, provider_enabled

    entry = next(p for p in list_providers() if p["id"] == "swaraj")
    assert entry["enabled"] is False
    assert BLOCKED_CODE in entry["reason"]
    assert get_provider("swaraj") is None
    assert provider_enabled("swaraj")[0] is False


def test_swaraj_declares_no_config_fields_because_none_are_documented():
    assert SwarajCloudProvider().config_fields == ()
    assert SwarajCloudProvider().connection_provider is None


# ── The revision service ─────────────────────────────────────────────────────


@pytest.fixture(name="deployment")
def deployment_fixture(session, test_org):
    deployment = Deployment(
        org_id=test_org.id,
        name="Petstore",
        slug="petstore",
        provider="docker",
        status="running",
        url="http://127.0.0.1:49523/mcp",
        package_zip=b"v1-package",
        tool_count=3,
        provider_state_json=json.dumps({"host_port": "49523"}),
    )
    session.add(deployment)
    session.commit()
    session.refresh(deployment)
    return deployment


def test_revision_numbers_are_monotonic(session, deployment):
    assert service.next_revision_number(session, deployment.id) == 1
    service.record_revision(
        session, deployment, revision=1, origin=ORIGIN_CREATE, package_zip=b"v1"
    )
    session.commit()
    assert service.next_revision_number(session, deployment.id) == 2


def test_a_revision_keeps_the_package_and_its_digest(session, deployment):
    entry = service.record_revision(
        session, deployment, revision=1, origin=ORIGIN_CREATE, package_zip=b"v1-package"
    )
    session.commit()
    assert entry.package_zip == b"v1-package"
    assert entry.package_sha256 == service.package_digest(b"v1-package")
    assert len(entry.package_sha256) == 64


def test_settling_a_revision_supersedes_the_previous_active_one(session, deployment, monkeypatch):
    monkeypatch.setattr(service.db, "engine", session.get_bind())
    for revision in (1, 2):
        service.record_revision(
            session,
            deployment,
            revision=revision,
            origin=ORIGIN_CREATE if revision == 1 else ORIGIN_UPDATE,
            package_zip=f"v{revision}".encode(),
        )
    session.commit()

    service._settle_revision(deployment.id, 1, outcome=OUTCOME_ACTIVE, state={"url": "a"})
    service._settle_revision(deployment.id, 2, outcome=OUTCOME_ACTIVE, state={"url": "b"})

    session.expire_all()
    by_number = {e.revision: e for e in service.list_revisions(session, deployment.id)}
    assert by_number[1].outcome == OUTCOME_SUPERSEDED
    assert by_number[2].outcome == OUTCOME_ACTIVE
    assert by_number[2].url == "b"


def test_a_failed_revision_does_not_supersede_the_running_one(session, deployment, monkeypatch):
    monkeypatch.setattr(service.db, "engine", session.get_bind())
    for revision in (1, 2):
        service.record_revision(
            session, deployment, revision=revision, origin=ORIGIN_CREATE, package_zip=b"p"
        )
    session.commit()
    service._settle_revision(deployment.id, 1, outcome=OUTCOME_ACTIVE, state={"url": "a"})
    service._settle_revision(deployment.id, 2, outcome=OUTCOME_FAILED, error="build failed")

    session.expire_all()
    by_number = {e.revision: e for e in service.list_revisions(session, deployment.id)}
    assert by_number[1].outcome == OUTCOME_ACTIVE, "a failed update must not unseat what is running"
    assert by_number[2].error == "build failed"


def test_history_is_newest_first(session, deployment):
    for revision in (1, 2, 3):
        service.record_revision(
            session, deployment, revision=revision, origin=ORIGIN_CREATE, package_zip=b"p"
        )
    session.commit()
    assert [e.revision for e in service.list_revisions(session, deployment.id)] == [3, 2, 1]


def test_a_rollback_revision_records_what_it_restored(session, deployment):
    service.record_revision(
        session,
        deployment,
        revision=3,
        origin=ORIGIN_ROLLBACK,
        package_zip=b"v1-package",
        restored_from_revision=1,
    )
    session.commit()
    entry = service.get_revision(session, deployment.id, 3)
    assert entry.origin == ORIGIN_ROLLBACK
    assert entry.restored_from_revision == 1
    # The restored artifact is the one that ran, byte for byte.
    assert entry.package_zip == b"v1-package"


async def test_metrics_are_not_collected_for_a_deployment_that_never_started(session, deployment):
    deployment.status = "failed"
    session.add(deployment)
    session.commit()
    metrics = await service.collect_metrics(session, deployment)
    assert isinstance(metrics, ProviderMetrics)
    assert metrics.unavailable_reason == "The deployment is failed."


async def test_a_provider_error_during_metrics_is_reported_not_raised(
    session, deployment, monkeypatch
):
    async def explode(*args, **kwargs):
        raise RuntimeError("cloud unreachable")

    monkeypatch.setattr("sutr.services.deployments.resolve_target", explode)
    metrics = await service.collect_metrics(session, deployment)
    assert "cloud unreachable" in metrics.unavailable_reason
