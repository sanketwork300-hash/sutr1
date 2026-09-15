"""Continuous sync and the apply policy (LLD §3.3, build prompt §13).

The rule under test throughout: **breaking changes must not automatically
deploy unless policy permits it.** A watcher that quietly applies a breaking
change is worse than no watcher, because the failure arrives without anybody
having decided anything.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlmodel import select

from sutr.events import topics
from sutr.models.api_source import (
    APPLY_ALWAYS,
    APPLY_NEVER,
    APPLY_NON_BREAKING,
    ROLE_COMPARISON,
    ROLE_PRIMARY,
    ApiSource,
)
from sutr.models.drift_report import STATUS_APPLIED, STATUS_OPEN, DriftReport
from sutr.models.openapi_project import OpenAPIProject
from sutr.models.outbox_event import OutboxEvent
from sutr.openapi import fingerprint
from sutr.openapi.normalizer import normalize
from sutr.services import source_sync
from sutr.source_connectors.base import FetchResult, Provenance

BASE = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/pets/{id}": {
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete a pet.",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"204": {"description": "gone"}},
            }
        },
    },
}


def _spec(**mutation) -> str:
    document = json.loads(json.dumps(BASE))
    if mutation.get("remove_delete"):
        del document["paths"]["/pets/{id}"]
    if mutation.get("new_summary"):
        document["paths"]["/pets"]["get"]["summary"] = mutation["new_summary"]
    if mutation.get("add_auth"):
        document["components"] = {
            "securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Key"}}
        }
        document["security"] = [{"k": []}]
    if mutation.get("add_operation"):
        document["paths"]["/pets/search"] = {
            "get": {
                "operationId": "searchPets",
                "summary": "Search.",
                "responses": {"200": {"description": "ok"}},
            }
        }
    if mutation.get("reformat"):
        # Same API, different bytes.
        return json.dumps(document, indent=4, sort_keys=True)
    return json.dumps(document)


@pytest.fixture(name="project")
def project_fixture(session, test_org):
    definition = normalize(json.loads(_spec()))
    project = OpenAPIProject(
        org_id=test_org.id,
        name="Pets",
        source_kind="url",
        source_url="https://api.example.com/openapi.json",
        spec_text=_spec(),
        ir_json=definition.model_dump_json(),
        ir_hash=fingerprint.fingerprint(definition),
        ir_version=definition.ir_version,
        content_hash=fingerprint.content_hash(_spec()),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _source(session, test_org, project, **overrides) -> ApiSource:
    definition = normalize(json.loads(_spec()))
    source = ApiSource(
        org_id=test_org.id,
        project_id=project.id,
        connector="url",
        role=overrides.pop("role", ROLE_PRIMARY),
        label="Primary",
        config_json=json.dumps(
            {"url": overrides.get("source_uri", "https://api.example.com/openapi.json")}
        ),
        source_uri=overrides.pop("source_uri", "https://api.example.com/openapi.json"),
        ir_json=definition.model_dump_json(),
        ir_hash=fingerprint.fingerprint(definition),
        content_hash=fingerprint.content_hash(_spec()),
        watch_enabled=overrides.pop("watch_enabled", True),
        apply_policy=overrides.pop("apply_policy", APPLY_NEVER),
        **overrides,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _fetch_returning(content: str | None, *, not_modified: bool = False):
    async def fake_fetch(self, config, secrets, *, known=None):
        return FetchResult(
            content=content,
            provenance=Provenance(
                source_type="url",
                source_uri=config.get("url", ""),
                etag='"new"',
                retrieved_at=datetime.utcnow().isoformat(),
            ),
            not_modified=not_modified,
        )

    return patch("sutr.source_connectors.url.UrlConnector.fetch", fake_fetch)


# ── Nothing changed ──────────────────────────────────────────────────────────


async def test_a_not_modified_response_is_not_a_change(session, test_org, project):
    source = _source(session, test_org, project)
    with _fetch_returning(None, not_modified=True):
        outcome = await source_sync.check_source(session, source)
    assert outcome.status == "unchanged"
    assert session.exec(select(DriftReport)).all() == []


async def test_identical_bytes_are_not_a_change(session, test_org, project):
    source = _source(session, test_org, project)
    with _fetch_returning(_spec()):
        outcome = await source_sync.check_source(session, source)
    assert outcome.status == "unchanged"


async def test_a_reformatted_document_is_not_drift(session, test_org, project):
    """The file moved; the API did not. Reporting this as drift would train
    people to ignore drift reports."""
    source = _source(session, test_org, project)
    with _fetch_returning(_spec(reformat=True)):
        outcome = await source_sync.check_source(session, source)
    assert outcome.status == "unchanged"
    assert "the API it describes did not" in outcome.detail
    assert session.exec(select(DriftReport)).all() == []


# ── The apply policy ─────────────────────────────────────────────────────────


async def test_a_breaking_change_is_never_applied_under_the_default_policy(
    session, test_org, project
):
    source = _source(session, test_org, project, apply_policy=APPLY_NEVER)
    original_ir = project.ir_hash
    with _fetch_returning(_spec(remove_delete=True)):
        outcome = await source_sync.check_source(session, source)

    assert outcome.status == "changed"
    assert outcome.applied is False
    assert outcome.breaking == 1
    report = session.exec(select(DriftReport)).one()
    assert report.status == STATUS_OPEN
    assert report.auto_applied is False
    assert "waits for a human" in report.withheld_reason
    session.refresh(project)
    assert project.ir_hash == original_ir, "the project must not have moved"


async def test_a_breaking_change_is_withheld_under_the_non_breaking_policy(
    session, test_org, project
):
    source = _source(session, test_org, project, apply_policy=APPLY_NON_BREAKING)
    with _fetch_returning(_spec(remove_delete=True)):
        outcome = await source_sync.check_source(session, source)
    assert outcome.applied is False
    report = session.exec(select(DriftReport)).one()
    assert "breaking change(s) were detected" in report.withheld_reason


async def test_a_non_breaking_change_is_applied_under_that_policy(session, test_org, project):
    source = _source(session, test_org, project, apply_policy=APPLY_NON_BREAKING)
    with _fetch_returning(_spec(add_operation=True)):
        outcome = await source_sync.check_source(session, source)

    assert outcome.applied is True
    assert outcome.breaking == 0
    report = session.exec(select(DriftReport)).one()
    assert report.status == STATUS_APPLIED
    session.refresh(project)
    assert "searchPets" in project.ir_json


async def test_a_security_change_always_waits_for_a_human(session, test_org, project):
    """Whichever direction it moves: adding auth breaks callers, removing it
    makes the API public."""
    source = _source(session, test_org, project, apply_policy=APPLY_NON_BREAKING)
    with _fetch_returning(_spec(add_auth=True)):
        outcome = await source_sync.check_source(session, source)
    assert outcome.applied is False
    report = session.exec(select(DriftReport)).one()
    assert "Security changes always wait" in report.withheld_reason


async def test_the_always_policy_applies_a_breaking_change(session, test_org, project):
    """It exists, it is not the default, and choosing it is a decision."""
    source = _source(session, test_org, project, apply_policy=APPLY_ALWAYS)
    with _fetch_returning(_spec(remove_delete=True)):
        outcome = await source_sync.check_source(session, source)
    assert outcome.applied is True
    assert outcome.breaking == 1
    session.refresh(project)
    assert "deletePet" not in project.ir_json


async def test_a_comparison_source_never_becomes_the_definition(session, test_org, project):
    source = _source(session, test_org, project, role=ROLE_COMPARISON, apply_policy=APPLY_ALWAYS)
    original = project.ir_hash
    with _fetch_returning(_spec(add_operation=True)):
        outcome = await source_sync.check_source(session, source)
    assert outcome.applied is False
    report = session.exec(select(DriftReport)).one()
    assert "comparison source" in report.withheld_reason
    session.refresh(project)
    assert project.ir_hash == original


# ── Applying does not deploy ─────────────────────────────────────────────────


async def test_applying_updates_the_definition_but_does_not_recompile(session, test_org, project):
    """Sync's job ends at 'the definition is current'. Turning that into tools
    is a compile, and into a runtime is a deployment — each with its own gate."""
    project.integration_db_id = None
    session.add(project)
    session.commit()

    source = _source(session, test_org, project, apply_policy=APPLY_NON_BREAKING)
    with _fetch_returning(_spec(add_operation=True)):
        await source_sync.check_source(session, source)

    session.refresh(project)
    assert project.integration_db_id is None
    assert project.status == "imported"


# ── Events ───────────────────────────────────────────────────────────────────


async def test_a_change_announces_itself(session, test_org, project):
    source = _source(session, test_org, project)
    with _fetch_returning(_spec(remove_delete=True)):
        await source_sync.check_source(session, source)
    types = {row.event_type for row in session.exec(select(OutboxEvent)).all()}
    assert topics.SOURCE_CHANGED in types
    assert topics.SOURCE_DRIFT_DETECTED in types


async def test_the_drift_event_carries_the_decision(session, test_org, project):
    source = _source(session, test_org, project, apply_policy=APPLY_NEVER)
    with _fetch_returning(_spec(remove_delete=True)):
        await source_sync.check_source(session, source)
    row = session.exec(
        select(OutboxEvent).where(OutboxEvent.event_type == topics.SOURCE_DRIFT_DETECTED)
    ).one()
    payload = json.loads(row.envelope_json)["payload"]
    assert payload["breaking"] == 1
    assert payload["auto_applied"] is False
    assert payload["withheld_reason"]


async def test_an_unchanged_source_announces_nothing(session, test_org, project):
    source = _source(session, test_org, project)
    with _fetch_returning(None, not_modified=True):
        await source_sync.check_source(session, source)
    assert session.exec(select(OutboxEvent)).all() == []


# ── Failure handling ─────────────────────────────────────────────────────────


async def test_a_fetch_failure_is_recorded_and_backed_off(session, test_org, project):
    from sutr.source_connectors.base import ConnectorError

    source = _source(session, test_org, project)

    async def failing_fetch(self, config, secrets, *, known=None):
        raise ConnectorError("fetch_failed", "The URL returned HTTP 503.")

    with patch("sutr.source_connectors.url.UrlConnector.fetch", failing_fetch):
        outcome = await source_sync.check_source(session, source)

    assert outcome.status == "failed"
    session.refresh(source)
    assert source.consecutive_failures == 1
    assert "503" in source.last_error
    assert source.watch_enabled is True


async def test_persistent_failure_pauses_watching_and_says_so(session, test_org, project):
    from sutr.source_connectors.base import ConnectorError

    source = _source(session, test_org, project)
    source.consecutive_failures = source_sync.FAILURE_PAUSE_THRESHOLD - 1
    session.add(source)
    session.commit()

    async def failing_fetch(self, config, secrets, *, known=None):
        raise ConnectorError("fetch_failed", "gone")

    with patch("sutr.source_connectors.url.UrlConnector.fetch", failing_fetch):
        await source_sync.check_source(session, source)

    session.refresh(source)
    assert source.watch_enabled is False
    assert "watching paused" in source.last_error


async def test_a_change_that_no_longer_translates_is_a_failure_not_a_silent_apply(
    session, test_org, project
):
    source = _source(session, test_org, project, apply_policy=APPLY_ALWAYS)
    original = project.ir_hash
    with _fetch_returning('{"openapi": "3.0.0", "info": {}}'):
        outcome = await source_sync.check_source(session, source)
    assert outcome.status == "failed"
    assert "not usable" in outcome.detail
    session.refresh(project)
    assert project.ir_hash == original


# ── Backoff and scheduling ───────────────────────────────────────────────────


def test_only_watched_sources_are_due(session, test_org, project):
    watched = _source(session, test_org, project)
    unwatched = _source(
        session,
        test_org,
        project,
        role=ROLE_COMPARISON,
        watch_enabled=False,
        source_uri="https://other.example.com/openapi.json",
    )
    unwatched.config_json = json.dumps({"url": "https://other.example.com/openapi.json"})
    session.add(unwatched)
    session.commit()

    due = source_sync.due_sources(session)
    assert [s.id for s in due] == [watched.id]


def test_a_recently_checked_source_is_not_due_again(session, test_org, project):
    source = _source(session, test_org, project)
    source.last_checked_at = datetime.utcnow()
    source.watch_interval_seconds = 3600
    session.add(source)
    session.commit()
    assert source_sync.due_sources(session) == []


def test_a_failing_source_is_polled_less_often(session, test_org, project):
    """Hammering a source that is down does not help it or us."""
    source = _source(session, test_org, project)
    source.watch_interval_seconds = 60
    source.last_checked_at = datetime.utcnow() - timedelta(seconds=120)
    source.consecutive_failures = 4  # 2**4 = 16x backoff
    session.add(source)
    session.commit()
    assert source_sync.due_sources(session) == []

    source.last_checked_at = datetime.utcnow() - timedelta(seconds=60 * 16 + 10)
    session.add(source)
    session.commit()
    assert len(source_sync.due_sources(session)) == 1


def test_backoff_is_capped(session, test_org, project):
    source = _source(session, test_org, project)
    source.watch_interval_seconds = 60
    source.consecutive_failures = 50
    source.last_checked_at = datetime.utcnow() - timedelta(
        seconds=60 * source_sync.MAX_BACKOFF_MULTIPLIER + 10
    )
    session.add(source)
    session.commit()
    assert len(source_sync.due_sources(session)) == 1


# ── Comparing two sources ────────────────────────────────────────────────────


def test_two_sources_that_disagree_produce_a_comparison_report(session, test_org, project):
    """The LLD's governance differentiator: the repository and the gateway have
    diverged, and you hear it from the platform rather than from a caller."""
    primary = _source(session, test_org, project)
    other = _source(
        session,
        test_org,
        project,
        role=ROLE_COMPARISON,
        source_uri="https://gateway.example.com/openapi.json",
    )
    diverged = normalize(json.loads(_spec(remove_delete=True)))
    other.ir_json = diverged.model_dump_json()
    other.ir_hash = fingerprint.fingerprint(diverged)
    session.add(other)
    session.commit()

    report = source_sync.compare_sources(session, primary, other)
    session.commit()
    assert report is not None
    assert report.breaking_count == 1
    assert report.compared_source_id == primary.id
    assert "never applied" in report.withheld_reason


def test_comparison_needs_both_sides_to_have_been_fetched(session, test_org, project):
    primary = _source(session, test_org, project)
    other = _source(
        session,
        test_org,
        project,
        role=ROLE_COMPARISON,
        source_uri="https://gateway.example.com/openapi.json",
    )
    other.ir_json = None
    session.add(other)
    session.commit()
    assert source_sync.compare_sources(session, primary, other) is None
