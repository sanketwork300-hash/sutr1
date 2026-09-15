"""Sources: where a project's definition comes from, and whether it still matches.

The LLD's onboarding question is *"Where does your API definition currently
live?"* (§3.3), and its two-phase model is bootstrap-then-watch. These endpoints
are the second half: attach sources to a project, watch them, and see what has
drifted.

Applying a drift report is deliberately a separate, explicit action. Build
prompt §13: breaking changes must not deploy automatically unless policy says
so — which means there has to be somewhere a human says so.
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlmodel import Session, col, select

from sutr import events
from sutr.authz import ensure_agent_can
from sutr.common import Page, PageRequest, envelope
from sutr.common.errors import ConflictError, InvalidRequestError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.events import topics
from sutr.models.api_source import (
    APPLY_POLICIES,
    ROLE_PRIMARY,
    ROLES,
    ApiSource,
)
from sutr.models.drift_report import (
    ORIGIN_MANUAL,
    STATUS_APPLIED,
    STATUS_DISMISSED,
    STATUS_OPEN,
    DriftReport,
)
from sutr.models.openapi_project import OpenAPIProject
from sutr.openapi import fingerprint
from sutr.openapi.pipeline import translate
from sutr.secrets.records import delete_secret, upsert_secret
from sutr.services import source_sync
from sutr.services.audit import actor_from_agent_auth, record_audit
from sutr.source_connectors import registry as connector_registry
from sutr.source_connectors.base import ConnectorError

router = APIRouter(prefix="/v1/sources", tags=["sources"])


class CreateSourceRequest(BaseModel):
    project_id: uuid.UUID
    connector: str
    config: dict = {}
    label: str = Field(default="", max_length=120)
    role: str = ROLE_PRIMARY
    # A token or API key for this source. Stored through the secrets backend;
    # never echoed back.
    token: str | None = None
    watch_enabled: bool = False
    watch_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    apply_policy: str = "never"


class UpdateSourceRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    config: dict | None = None
    token: str | None = None
    watch_enabled: bool | None = None
    watch_interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    apply_policy: str | None = None


class DiscoverRequest(BaseModel):
    connector: str
    config: dict = {}
    token: str | None = None


class ValidateRequest(BaseModel):
    content: str


def _serialize_source(source: ApiSource, *, connector=None) -> dict:
    plan = None
    if connector is not None:
        plan = connector.watch_plan(json.loads(source.config_json or "{}"))
    return {
        "id": str(source.id),
        "project_id": str(source.project_id),
        "connector": source.connector,
        "role": source.role,
        "label": source.label,
        # Config, never credentials: the token lives in a secret row.
        "config": json.loads(source.config_json or "{}"),
        "has_token": source.token_secret_id is not None,
        "provenance": {
            "source_uri": source.source_uri,
            "source_version": source.source_version,
            "commit_sha": source.commit_sha,
            "etag": source.etag,
            "last_modified": source.last_modified,
            "retrieved_at": source.retrieved_at.isoformat() if source.retrieved_at else None,
            "content_hash": source.content_hash,
            "ir_hash": source.ir_hash,
        },
        "watch": {
            "enabled": source.watch_enabled,
            "interval_seconds": source.watch_interval_seconds,
            "apply_policy": source.apply_policy,
            "last_checked_at": (
                source.last_checked_at.isoformat() if source.last_checked_at else None
            ),
            "last_changed_at": (
                source.last_changed_at.isoformat() if source.last_changed_at else None
            ),
            "consecutive_failures": source.consecutive_failures,
            "last_error": source.last_error,
            "plan": (
                {
                    "mode": plan.mode,
                    "conditional": plan.conditional,
                    "interval_seconds": plan.interval_seconds,
                    "reason": plan.reason,
                }
                if plan
                else None
            ),
        },
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat(),
    }


def _serialize_drift(report: DriftReport) -> dict:
    return {
        "id": str(report.id),
        "project_id": str(report.project_id),
        "source_id": str(report.source_id),
        "compared_source_id": (
            str(report.compared_source_id) if report.compared_source_id else None
        ),
        "origin": report.origin,
        "status": report.status,
        "counts": {
            "BREAKING": report.breaking_count,
            "NON_BREAKING": report.non_breaking_count,
            "SECURITY": report.security_count,
            "DOCUMENTATION": report.documentation_count,
            "METADATA": report.metadata_count,
        },
        "changes": json.loads(report.changes_json or "[]"),
        "before_ir_hash": report.before_ir_hash,
        "after_ir_hash": report.after_ir_hash,
        "auto_applied": report.auto_applied,
        "withheld_reason": report.withheld_reason,
        "detected_at": report.detected_at.isoformat(),
        "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
    }


def _load_project(session: Session, project_id: uuid.UUID, org_id: uuid.UUID) -> OpenAPIProject:
    project = session.get(OpenAPIProject, project_id)
    if project is None or project.org_id != org_id:
        raise NotFoundError("OpenAPI project not found.")
    return project


def _load_source(session: Session, source_id: uuid.UUID, org_id: uuid.UUID) -> ApiSource:
    source = session.get(ApiSource, source_id)
    if source is None or source.org_id != org_id:
        raise NotFoundError("Source not found.")
    return source


# ── Connectors ───────────────────────────────────────────────────────────────


@router.get("/connectors")
def list_connectors(_: AgentAuth = Depends(get_agent_auth)) -> dict:
    """Every source type, including the ones declared but not built.

    A source type that is silently missing looks like an oversight; one listed
    as unavailable with a reason is a decision somebody made.
    """
    return envelope(connector_registry.describe_all())


@router.post("/discover")
async def discover(
    body: DiscoverRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What definitions live at a source, before committing to one."""
    ensure_agent_can(session, agent_auth, "integrations:manage")
    connector = _require_connector(body.connector)
    secrets = {"token": body.token, "api_key": body.token} if body.token else {}
    try:
        found = await connector.discover(body.config, secrets)
    except ConnectorError as exc:
        raise InvalidRequestError(exc.message, code=exc.code)
    return envelope({"candidates": [item.as_dict() for item in found]})


@router.post("/validate")
def validate_document(
    body: ValidateRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Translate a document and report every stage.

    The same pipeline an import runs, so what this says is what an import will
    do — including which stage would fail, and why.
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    return envelope(translate(body.content).as_dict())


def _require_connector(connector_id: str):
    connector = connector_registry.get(connector_id)
    if connector is None:
        planned = {entry.id: entry for entry in connector_registry.PLANNED}
        if connector_id in planned:
            raise InvalidRequestError(
                planned[connector_id].reason,
                code="connector_not_implemented",
                details={"connector": connector_id},
            )
        available = [c.id for c in connector_registry.all_connectors()]
        raise InvalidRequestError(
            f"No source connector '{connector_id}'.",
            details={"available": available},
        )
    return connector


# ── Sources ──────────────────────────────────────────────────────────────────


@router.get("")
def list_sources(
    project_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    statement = select(ApiSource).where(ApiSource.org_id == agent_auth.org.id)
    if project_id:
        statement = statement.where(ApiSource.project_id == project_id)
    sources = session.exec(statement.order_by(col(ApiSource.created_at))).all()
    return envelope(
        [
            _serialize_source(source, connector=connector_registry.get(source.connector))
            for source in sources
        ]
    )


@router.post("", status_code=201)
async def create_source(
    body: CreateSourceRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Attach a source to a project, checking it is reachable first.

    Connecting before storing means a mistyped URL or a missing token is
    reported while the user is still looking at the form, rather than an hour
    later in a sync log nobody reads.
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    project = _load_project(session, body.project_id, agent_auth.org.id)
    connector = _require_connector(body.connector)

    if body.role not in ROLES:
        raise InvalidRequestError(f"role must be one of {list(ROLES)}.")
    if body.apply_policy not in APPLY_POLICIES:
        raise InvalidRequestError(f"apply_policy must be one of {list(APPLY_POLICIES)}.")
    if body.watch_enabled and not connector.supports_watch:
        raise InvalidRequestError(
            f"The {connector.display_name} source cannot be watched: "
            f"{connector.watch_plan(body.config).reason}",
            details={"connector": body.connector},
        )

    secrets = {"token": body.token, "api_key": body.token} if body.token else {}
    try:
        connection = await connector.connect(body.config, secrets)
    except ConnectorError as exc:
        raise InvalidRequestError(exc.message, code=exc.code)
    if not connection.connected:
        raise InvalidRequestError(connection.message, code="source_unreachable")

    source = ApiSource(
        org_id=agent_auth.org.id,
        project_id=project.id,
        connector=body.connector,
        role=body.role,
        label=body.label or connector.display_name,
        config_json=json.dumps(body.config),
        watch_enabled=body.watch_enabled,
        watch_interval_seconds=body.watch_interval_seconds,
        apply_policy=body.apply_policy,
        source_uri=str(body.config.get("url") or ""),
    )
    if body.token:
        secret = upsert_secret(
            session,
            org_id=agent_auth.org.id,
            kind="api_source_token",
            ref=f"sources/{agent_auth.org.id}/{source.id}/token",
            value=body.token,
        )
        source.token_secret_id = secret.id
    session.add(source)
    session.flush()

    events.publish(
        session,
        topics.SOURCE_CONNECTED,
        tenant_id=agent_auth.org.id,
        resource_id=str(project.id),
        producer="source-connectors",
        payload={
            "source_id": str(source.id),
            "connector": body.connector,
            "role": body.role,
            "watch_enabled": body.watch_enabled,
        },
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="source.connected",
        summary=f"Source '{source.label}' ({body.connector}) attached to '{project.name}'",
        target_type="api_source",
        target_id=str(source.id),
        metadata={"connector": body.connector, "role": body.role},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(source)

    # Populate the baseline so the first drift check has a "before".
    await source_sync.check_source(session, source, origin=ORIGIN_MANUAL, force=True)
    session.refresh(source)
    return envelope(_serialize_source(source, connector=connector))


@router.get("/{source_id}")
def get_source(
    source_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    source = _load_source(session, source_id, agent_auth.org.id)
    return envelope(_serialize_source(source, connector=connector_registry.get(source.connector)))


@router.patch("/{source_id}")
def update_source(
    source_id: uuid.UUID,
    body: UpdateSourceRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    source = _load_source(session, source_id, agent_auth.org.id)
    connector = connector_registry.get(source.connector)

    if body.apply_policy is not None:
        if body.apply_policy not in APPLY_POLICIES:
            raise InvalidRequestError(f"apply_policy must be one of {list(APPLY_POLICIES)}.")
        source.apply_policy = body.apply_policy
    if body.watch_enabled is not None:
        if body.watch_enabled and connector is not None and not connector.supports_watch:
            raise InvalidRequestError(
                f"The {connector.display_name} source cannot be watched: "
                f"{connector.watch_plan(json.loads(source.config_json or '{}')).reason}"
            )
        source.watch_enabled = body.watch_enabled
        if body.watch_enabled:
            # Re-enabling clears the backoff: the operator is asserting the
            # source is healthy again.
            source.consecutive_failures = 0
            source.last_error = None
    if body.watch_interval_seconds is not None:
        source.watch_interval_seconds = body.watch_interval_seconds
    if body.label is not None:
        source.label = body.label
    if body.config is not None:
        source.config_json = json.dumps(body.config)
        source.source_uri = str(body.config.get("url") or source.source_uri)
    if body.token is not None:
        secret = upsert_secret(
            session,
            org_id=agent_auth.org.id,
            kind="api_source_token",
            ref=f"sources/{agent_auth.org.id}/{source.id}/token",
            value=body.token,
            secret_id=source.token_secret_id,
        )
        source.token_secret_id = secret.id

    source.updated_at = datetime.utcnow()
    session.add(source)
    session.commit()
    session.refresh(source)
    return envelope(_serialize_source(source, connector=connector))


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    source = _load_source(session, source_id, agent_auth.org.id)

    connector = connector_registry.get(source.connector)
    if connector is not None:
        try:
            await connector.disconnect(
                json.loads(source.config_json or "{}"),
                source_sync.secrets_for(session, source),
            )
        except ConnectorError:
            # Failing to release something on the source's side must not block
            # removing it from ours; the row is the thing the user asked to go.
            pass

    # Drift reports reference the source, so they go with it.
    for report in session.exec(select(DriftReport).where(DriftReport.source_id == source.id)).all():
        session.delete(report)
    delete_secret(session, source.token_secret_id)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="source.disconnected",
        summary=f"Source '{source.label}' ({source.connector}) removed",
        target_type="api_source",
        target_id=str(source.id),
        **actor_from_agent_auth(agent_auth),
    )
    session.delete(source)
    session.commit()


@router.post("/{source_id}/check")
async def check_now(
    source_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Fetch the source now and report what changed."""
    ensure_agent_can(session, agent_auth, "integrations:manage")
    source = _load_source(session, source_id, agent_auth.org.id)
    outcome = await source_sync.check_source(session, source, origin=ORIGIN_MANUAL, force=True)
    return envelope(
        {
            "status": outcome.status,
            "detail": outcome.detail,
            "breaking": outcome.breaking,
            "applied": outcome.applied,
            "drift_report_id": str(outcome.report_id) if outcome.report_id else None,
        }
    )


@router.post("/{source_id}/compare/{other_source_id}")
def compare_two(
    source_id: uuid.UUID,
    other_source_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Report where two attached sources disagree.

    The LLD's governance differentiator (§3.3): a team whose source of truth is
    a repository while a gateway also publishes a definition finds out the two
    have diverged, rather than hearing it from a caller.
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    primary = _load_source(session, source_id, agent_auth.org.id)
    other = _load_source(session, other_source_id, agent_auth.org.id)
    if primary.project_id != other.project_id:
        raise InvalidRequestError("Both sources must belong to the same project.")
    if primary.id == other.id:
        raise InvalidRequestError("A source cannot be compared with itself.")

    report = source_sync.compare_sources(session, primary, other)
    if report is None:
        raise ConflictError(
            "Both sources must have been fetched at least once before they can be compared.",
            details={"hint": "Run a check on each source first."},
        )
    session.commit()
    session.refresh(report)
    return envelope(_serialize_drift(report))


# ── Drift ────────────────────────────────────────────────────────────────────


@router.get("/{source_id}/drift")
def list_drift(
    source_id: uuid.UUID,
    status: str | None = None,
    limit: int | None = Query(default=None),
    cursor: str | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    source = _load_source(session, source_id, agent_auth.org.id)
    page_request = PageRequest.parse(limit=limit, cursor=cursor)

    statement = select(DriftReport).where(DriftReport.source_id == source.id)
    if status:
        statement = statement.where(DriftReport.status == status)
    if page_request.cursor and "detected_at" in page_request.cursor:
        # Parsed back into a datetime rather than compared as text: a string
        # comparison against a DateTime column is dialect-dependent, and the
        # dialect that gets it wrong returns overlapping pages.
        try:
            after = datetime.fromisoformat(str(page_request.cursor["detected_at"]))
        except ValueError:
            raise InvalidRequestError(
                "The `cursor` parameter is not a cursor this API issued.",
                details={"parameter": "cursor"},
            )
        cursor_id = page_request.cursor.get("id")
        # Tie-broken by id, so reports recorded in the same instant paginate
        # without repeating or skipping one.
        tie_break = (
            and_(
                col(DriftReport.detected_at) == after,
                col(DriftReport.id) < uuid.UUID(str(cursor_id)),
            )
            if cursor_id
            else None
        )
        condition = col(DriftReport.detected_at) < after
        statement = statement.where(
            or_(condition, tie_break) if tie_break is not None else condition
        )
    rows = list(
        session.exec(
            statement.order_by(
                col(DriftReport.detected_at).desc(), col(DriftReport.id).desc()
            ).limit(page_request.fetch_limit)
        ).all()
    )
    page = Page.build(
        rows,
        page_request,
        cursor_for=lambda row: {
            "detected_at": row.detected_at.isoformat(),
            "id": str(row.id),
        },
    )
    return envelope(
        [_serialize_drift(report) for report in page.items], next_cursor=page.next_cursor
    )


@router.post("/drift/{report_id}/apply")
def apply_drift(
    report_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Take a withheld change into the project.

    This is where a human says yes to a breaking change — the decision build
    prompt §13 requires to exist. Applying updates the project's definition; it
    does **not** recompile or redeploy, because those have their own gates.
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    report = session.get(DriftReport, report_id)
    if report is None or report.org_id != agent_auth.org.id:
        raise NotFoundError("Drift report not found.")
    if report.status != STATUS_OPEN:
        raise ConflictError(f"This report is already {report.status}.")
    if not report.after_spec_text:
        raise ConflictError(
            "This report holds no document to apply — comparison reports describe a "
            "disagreement between two sources rather than a change to take."
        )

    translation = translate(report.after_spec_text)
    if not translation.ok:
        raise ConflictError(
            "The recorded document no longer translates: "
            f"{translation.failed_stage.error_message if translation.failed_stage else 'unknown'}"
        )

    project = _load_project(session, report.project_id, agent_auth.org.id)
    project.spec_text = report.after_spec_text
    project.ir_json = translation.definition.model_dump_json()
    project.ir_hash = translation.ir_hash
    project.ir_version = translation.ir_version
    project.content_hash = fingerprint.content_hash(report.after_spec_text)
    project.updated_at = datetime.utcnow()
    session.add(project)

    report.status = STATUS_APPLIED
    report.resolved_at = datetime.utcnow()
    report.resolved_by_user_id = agent_auth.user.id if agent_auth.user else None
    session.add(report)

    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="source.drift_applied",
        summary=(
            f"Drift applied to '{project.name}': {report.breaking_count} breaking, "
            f"{report.security_count} security change(s)"
        ),
        target_type="drift_report",
        target_id=str(report.id),
        metadata={"breaking": report.breaking_count, "security": report.security_count},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(report)
    return envelope(_serialize_drift(report))


@router.post("/drift/{report_id}/dismiss")
def dismiss_drift(
    report_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Record that a human looked and chose not to take the change."""
    ensure_agent_can(session, agent_auth, "integrations:manage")
    report = session.get(DriftReport, report_id)
    if report is None or report.org_id != agent_auth.org.id:
        raise NotFoundError("Drift report not found.")
    if report.status != STATUS_OPEN:
        raise ConflictError(f"This report is already {report.status}.")
    report.status = STATUS_DISMISSED
    report.resolved_at = datetime.utcnow()
    report.resolved_by_user_id = agent_auth.user.id if agent_auth.user else None
    session.add(report)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="source.drift_dismissed",
        summary=f"Drift report dismissed ({report.breaking_count} breaking change(s))",
        target_type="drift_report",
        target_id=str(report.id),
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(report)
    return envelope(_serialize_drift(report))
