"""Drift detection: the declared state against what the provider reports."""

import json

import pytest
from sqlmodel import select

from sutr.deploy.base import ProviderError, ProviderStatus
from sutr.deploy.drift import detect
from sutr.models.deployment import Deployment
from sutr.models.deployment_revision import DeploymentRevision

from .test_deployments_api import _create, fake_provider_fixture  # noqa: F401


@pytest.fixture(autouse=True)
def _drift_uses_the_fake_provider(monkeypatch, fake_provider):
    """`drift` resolves the provider through its own import of the registry."""
    monkeypatch.setattr("sutr.deploy.drift.get_provider", lambda pid: fake_provider)


async def _deployment(client, session) -> Deployment:
    await _create(client)
    session.expire_all()
    return session.exec(select(Deployment)).one()


async def test_a_deployment_that_matches_its_declaration_is_clean(client, session, fake_provider):
    deployment = await _deployment(client, session)
    report = await detect(session, deployment)
    assert report.drifted is False
    assert report.findings == []
    assert "matches what the platform declared" in report.summary()


async def test_a_provider_running_another_revision_is_drift(client, session, fake_provider):
    deployment = await _deployment(client, session)
    state = json.loads(deployment.provider_state_json)
    state["revision"] = 7
    deployment.provider_state_json = json.dumps(state)
    session.add(deployment)
    session.commit()

    report = await detect(session, deployment)
    finding = next(f for f in report.findings if f.code == "revision_mismatch")
    assert finding.desired == deployment.current_revision
    assert finding.observed == 7


async def test_a_provider_that_stopped_on_its_own_is_drift(client, session, fake_provider):
    deployment = await _deployment(client, session)
    fake_provider.status_result = ProviderStatus(state="not_found", detail="container gone")
    report = await detect(session, deployment)
    finding = next(f for f in report.findings if f.code == "state_mismatch")
    assert finding.desired == "running"
    assert finding.observed == "not_found"
    assert "container gone" in finding.detail


async def test_a_package_that_no_longer_matches_its_revision_is_drift(
    client, session, fake_provider
):
    deployment = await _deployment(client, session)
    deployment.package_zip = b"different bytes"
    session.add(deployment)
    session.commit()
    report = await detect(session, deployment)
    assert any(f.code == "package_mismatch" for f in report.findings)


async def test_a_deployment_that_never_reached_a_provider_is_not_checked(
    client, session, fake_provider
):
    deployment = await _deployment(client, session)
    deployment.provider_state_json = "{}"
    session.add(deployment)
    session.commit()
    report = await detect(session, deployment)
    assert report.checked is False
    assert report.drifted is False
    assert "never reached a provider" in report.unavailable_reason


async def test_an_unreachable_provider_is_reported_rather_than_read_as_clean(
    client, session, fake_provider, monkeypatch
):
    """ "Could not check" and "nothing wrong" must not look the same."""
    deployment = await _deployment(client, session)

    async def failing(state, target=None):
        raise ProviderError("the daemon is not running")

    monkeypatch.setattr(fake_provider, "status", failing)
    report = await detect(session, deployment)
    assert report.checked is False
    assert "the daemon is not running" in report.unavailable_reason


async def test_the_drift_endpoint_reports_it(client, session, fake_provider):
    deployment = await _deployment(client, session)
    fake_provider.status_result = ProviderStatus(state="stopped")
    response = await client.get(f"/api/deployments/{deployment.id}/drift")
    assert response.status_code == 200
    body = response.json()
    assert body["drifted"] is True
    assert [f["code"] for f in body["findings"]] == ["state_mismatch"]


async def test_another_tenants_deployment_cannot_be_checked(
    client, session, test_user, fake_provider
):
    from sutr.models.org import Org

    deployment = await _deployment(client, session)
    other = Org(name="Other", slug="other-drift", owner_user_id=test_user.id)
    session.add(other)
    session.flush()
    deployment.org_id = other.id
    session.add(deployment)
    session.commit()

    response = await client.get(f"/api/deployments/{deployment.id}/drift")
    assert response.status_code == 404


async def test_detection_changes_nothing(client, session, fake_provider):
    """Detection, not reconciliation: looking must not correct anything."""
    deployment = await _deployment(client, session)
    fake_provider.status_result = ProviderStatus(state="not_found")
    before = deployment.status
    await detect(session, deployment)
    session.expire_all()
    assert session.exec(select(Deployment)).one().status == before
    assert len(session.exec(select(DeploymentRevision)).all()) == 1


@pytest.mark.parametrize("state", ["running", "stopped"])
async def test_both_declared_states_are_compared(client, session, fake_provider, state):
    deployment = await _deployment(client, session)
    deployment.status = state
    session.add(deployment)
    session.commit()
    fake_provider.status_result = ProviderStatus(state=state)
    report = await detect(session, deployment)
    assert report.drifted is False
