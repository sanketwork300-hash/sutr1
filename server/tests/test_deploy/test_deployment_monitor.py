"""Deployment monitoring sweep: status reconciliation, runtime metering, gauges."""

import uuid

import pytest
from sqlmodel import select

from sutr.deploy.base import ProviderStatus
from sutr.maintenance import DEPLOYMENT_SAMPLE_MINUTES, sweep_deployments
from sutr.models.deployment import Deployment
from sutr.models.usage_event import KIND_DEPLOYMENT_RUNTIME, UsageEvent
from sutr.observability import metrics
from tests.test_deploy.test_deployments_api import FakeProvider


@pytest.fixture(name="fake_provider")
def fake_provider_fixture(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("sutr.services.deployments.get_provider", lambda pid: fake)
    return fake


def _deployment(org_id, status="running", name="dep") -> Deployment:
    return Deployment(
        org_id=org_id,
        name=name,
        slug=name,
        provider="docker",
        status=status,
        package_zip=b"zip",
        provider_state_json='{"container_name": "c1"}',
        tool_count=2,
    )


async def test_sweep_meters_running_deployments(session, test_org, fake_provider):
    session.add(_deployment(test_org.id, "running", "alive"))
    # "failed" is terminal — refresh_status never consults the provider for it,
    # so it stays failed while the fake provider reports everything running.
    session.add(_deployment(test_org.id, "failed", "broken"))
    session.commit()

    result = await sweep_deployments()

    assert result["running"] == 1
    assert result["failed"] == 1
    assert result["runtime_metered"] == 1  # only the running one accrues runtime

    session.expire_all()
    event = session.exec(select(UsageEvent).where(UsageEvent.kind == KIND_DEPLOYMENT_RUNTIME)).one()
    assert event.quantity == DEPLOYMENT_SAMPLE_MINUTES
    assert event.source == "system"


async def test_sweep_reconciles_status_from_provider(session, test_org, fake_provider):
    session.add(_deployment(test_org.id, "running", "drifted"))
    session.commit()
    fake_provider.status_result = ProviderStatus(state="stopped", detail="exited")

    result = await sweep_deployments()

    assert result["reconciled"] == 1
    assert result["runtime_metered"] == 0  # no longer running → not billed
    session.expire_all()
    assert session.exec(select(Deployment)).one().status == "stopped"


async def test_sweep_sets_deployment_gauge(session, test_org, fake_provider):
    session.add(_deployment(test_org.id, "running", "one"))
    session.add(_deployment(test_org.id, "failed", "two"))
    session.commit()

    await sweep_deployments()

    body = metrics.render()[0].decode()
    assert 'sutr_deployments{status="running"} 1.0' in body
    assert 'sutr_deployments{status="failed"} 1.0' in body


async def test_sweep_survives_provider_errors(session, test_org, monkeypatch):
    """A broken provider must not abort the sweep for other deployments."""

    class Exploding(FakeProvider):
        async def status(self, state, target=None):
            raise RuntimeError("daemon unreachable")

    monkeypatch.setattr("sutr.services.deployments.get_provider", lambda pid: Exploding())
    session.add(_deployment(test_org.id, "running", "unreachable"))
    session.commit()

    result = await sweep_deployments()

    # refresh_status swallows provider failures, so the row keeps its status
    # and the sweep still completes and meters it.
    assert result["running"] == 1
    session.expire_all()
    assert session.exec(select(Deployment)).one().status == "running"


async def test_sweep_with_no_deployments_is_quiet(session, test_org, fake_provider):
    result = await sweep_deployments()
    assert result == {"reconciled": 0, "runtime_metered": 0}
    assert session.exec(select(UsageEvent)).first() is None
    assert uuid.UUID(str(test_org.id))  # sanity: fixture wired
