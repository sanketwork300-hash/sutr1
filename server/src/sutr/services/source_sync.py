"""Continuous sync: watch a source, notice change, classify it, decide.

The LLD's two-phase model (§3.3): bootstrap once from one authoritative
source, then optionally *watch* it. What arrives here is the second phase, and
its most important rule comes from build prompt §13 — **"Breaking changes must
not automatically deploy unless policy permits it."**

So a check never applies a change on its own authority. It:

1. fetches conditionally, so an unchanged source costs almost nothing;
2. compares hashes to tell "the file moved" from "the API moved";
3. diffs the IRs and classifies every difference;
4. consults the source's `apply_policy` and either applies or **withholds with
   a reason**.

Step 4 is why a withheld change records `withheld_reason`: "nothing happened"
must be explicable, or the next person assumes the watcher is broken.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from sutr import db, events
from sutr.events import topics
from sutr.models.api_source import (
    APPLY_ALWAYS,
    APPLY_NEVER,
    APPLY_NON_BREAKING,
    ROLE_PRIMARY,
    ApiSource,
)
from sutr.models.drift_report import (
    ORIGIN_COMPARISON,
    ORIGIN_SYNC,
    STATUS_APPLIED,
    STATUS_OPEN,
    DriftReport,
)
from sutr.models.openapi_project import OpenAPIProject
from sutr.observability import log_context
from sutr.openapi import fingerprint
from sutr.openapi.diff import Category, compare
from sutr.openapi.errors import OpenAPIError
from sutr.openapi.loader import parse_spec_text
from sutr.openapi.normalizer import ApiDefinition, normalize
from sutr.secrets.records import get_secret_value
from sutr.source_connectors import registry as connector_registry
from sutr.source_connectors.base import ConnectorError, Provenance

logger = logging.getLogger(__name__)

# A source that keeps failing is backed off rather than polled at full rate
# forever: the failure is usually the source's, and hammering it does not help.
MAX_BACKOFF_MULTIPLIER = 16
# Consecutive failures after which watching is paused entirely. Recorded on the
# row so the reason is visible instead of the source just going quiet.
FAILURE_PAUSE_THRESHOLD = 20


@dataclass
class CheckOutcome:
    """What one check of one source found."""

    status: str  # "unchanged" | "changed" | "failed" | "skipped"
    detail: str = ""
    report_id: uuid.UUID | None = None
    applied: bool = False
    breaking: int = 0


def provenance_of(source: ApiSource) -> Provenance:
    return Provenance(
        source_type=source.connector,
        source_uri=source.source_uri,
        source_version=source.source_version,
        commit_sha=source.commit_sha,
        etag=source.etag,
        last_modified=source.last_modified,
    )


def secrets_for(session: Session, source: ApiSource) -> dict:
    """Credentials for one source, resolved fresh.

    Never cached on the source row: a token that lives on the row outlives its
    revocation.
    """
    resolved: dict[str, str] = {}
    if source.token_secret_id:
        value = get_secret_value(session, source.token_secret_id)
        if value:
            # Connectors name their credential differently; give both, since
            # each reads only the key it knows.
            resolved["token"] = value
            resolved["api_key"] = value
    return resolved


def definition_of(source: ApiSource) -> ApiDefinition | None:
    if not source.ir_json:
        return None
    try:
        return ApiDefinition.model_validate_json(source.ir_json)
    except ValueError:
        return None


def _record_provenance(source: ApiSource, provenance: Provenance) -> None:
    source.source_uri = provenance.source_uri or source.source_uri
    source.source_version = provenance.source_version
    source.commit_sha = provenance.commit_sha
    source.etag = provenance.etag
    source.last_modified = provenance.last_modified
    source.retrieved_at = datetime.utcnow()


def _counts(report) -> dict[str, int]:
    return report.counts()


def _should_apply(source: ApiSource, breaking: int, security: int) -> tuple[bool, str]:
    """Whether this change may be taken automatically, and why not if not."""
    if source.role != ROLE_PRIMARY:
        return False, (
            "This is a comparison source: it is watched to report drift against the "
            "primary source, and never becomes the project's definition."
        )
    if source.apply_policy == APPLY_NEVER:
        return False, "The source's apply policy is 'never': every change waits for a human."
    if source.apply_policy == APPLY_NON_BREAKING:
        if breaking:
            return False, (
                f"{breaking} breaking change(s) were detected and the apply policy is "
                "'non_breaking', so the change was not applied."
            )
        if security:
            return False, (
                f"{security} security change(s) were detected. Security changes always wait "
                "for a human, whichever direction they move: adding authentication breaks "
                "existing callers, and removing it makes the API public."
            )
        return True, ""
    if source.apply_policy == APPLY_ALWAYS:
        return True, ""
    return False, f"Unknown apply policy '{source.apply_policy}'."


async def check_source(
    session: Session, source: ApiSource, *, origin: str = ORIGIN_SYNC, force: bool = False
) -> CheckOutcome:
    """Fetch a source, compare it with what the platform holds, and decide.

    `force` skips the conditional-request shortcut, for a user who has pressed
    "check now" and wants an answer rather than a 304.
    """
    with log_context.bound(tenant_id=str(source.org_id), provider_id=str(source.project_id)):
        connector = connector_registry.get(source.connector)
        if connector is None:
            return _fail(session, source, f"No connector '{source.connector}'.")

        config = json.loads(source.config_json or "{}")
        secrets = secrets_for(session, source)
        known = None if force else provenance_of(source)

        try:
            result = await connector.fetch(config, secrets, known=known)
        except (ConnectorError, OpenAPIError) as exc:
            return _fail(session, source, getattr(exc, "message", str(exc)))
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("source %s check crashed", source.id)
            return _fail(session, source, f"Unexpected error: {exc}")

        source.consecutive_failures = 0
        source.last_error = None
        source.last_checked_at = datetime.utcnow()
        _record_provenance(source, result.provenance)

        if result.not_modified or result.content is None:
            session.add(source)
            session.commit()
            return CheckOutcome(status="unchanged", detail="The source reports no change.")

        content_hash = fingerprint.content_hash(result.content)
        if source.content_hash == content_hash:
            # Byte-identical: nothing to parse, nothing to diff.
            session.add(source)
            session.commit()
            return CheckOutcome(status="unchanged", detail="The document is unchanged.")

        try:
            definition = normalize(parse_spec_text(result.content))
        except OpenAPIError as exc:
            return _fail(
                session,
                source,
                f"The source changed but the new document is not usable: {exc.message}",
            )

        new_hash = fingerprint.fingerprint(definition)
        previous = definition_of(source)
        source.content_hash = content_hash

        if previous is not None and source.ir_hash == new_hash:
            # The file moved but the API did not — a reformat, a comment, a key
            # reordering. Recording this as drift would train people to ignore
            # drift reports.
            source.ir_json = definition.model_dump_json()
            session.add(source)
            session.commit()
            return CheckOutcome(
                status="unchanged",
                detail="The document changed but the API it describes did not.",
            )

        if previous is None:
            # The first fetch establishes the baseline. There is nothing to
            # drift *from* yet, so recording a drift report here would put an
            # empty one at the top of every source's history.
            source.ir_json = definition.model_dump_json()
            source.ir_hash = new_hash
            source.last_changed_at = datetime.utcnow()
            session.add(source)
            events.publish(
                session,
                topics.SOURCE_CHANGED,
                tenant_id=source.org_id,
                resource_id=str(source.project_id),
                producer="source-connectors",
                payload={
                    "source_id": str(source.id),
                    "connector": source.connector,
                    "ir_hash": new_hash,
                    "baseline": True,
                },
            )
            session.commit()
            return CheckOutcome(
                status="changed",
                detail=f"Baseline recorded: {len(definition.operations)} operation(s).",
            )

        report = compare(previous, definition)
        counts = _counts(report)
        breaking = counts.get(Category.BREAKING.value, 0)
        security = counts.get(Category.SECURITY.value, 0)

        applied, withheld_reason = _should_apply(source, breaking, security)

        drift = DriftReport(
            org_id=source.org_id,
            project_id=source.project_id,
            source_id=source.id,
            origin=origin,
            status=STATUS_APPLIED if applied else STATUS_OPEN,
            breaking_count=breaking,
            non_breaking_count=counts.get(Category.NON_BREAKING.value, 0),
            security_count=security,
            documentation_count=counts.get(Category.DOCUMENTATION.value, 0),
            metadata_count=counts.get(Category.METADATA.value, 0),
            changes_json=json.dumps([change.as_dict() for change in report.changes]),
            before_ir_hash=source.ir_hash,
            after_ir_hash=new_hash,
            after_spec_text=result.content,
            auto_applied=applied,
            withheld_reason=withheld_reason or None,
            resolved_at=datetime.utcnow() if applied else None,
        )
        session.add(drift)

        source.ir_json = definition.model_dump_json()
        source.ir_hash = new_hash
        source.last_changed_at = datetime.utcnow()
        session.add(source)

        events.publish(
            session,
            topics.SOURCE_CHANGED,
            tenant_id=source.org_id,
            resource_id=str(source.project_id),
            producer="source-connectors",
            payload={
                "source_id": str(source.id),
                "connector": source.connector,
                "ir_hash": new_hash,
                "counts": counts,
            },
        )
        if report.has_changes:
            events.publish(
                session,
                topics.SOURCE_DRIFT_DETECTED,
                tenant_id=source.org_id,
                resource_id=str(source.project_id),
                producer="source-connectors",
                payload={
                    "source_id": str(source.id),
                    "drift_report_id": str(drift.id),
                    "counts": counts,
                    "breaking": breaking,
                    "auto_applied": applied,
                    "withheld_reason": withheld_reason or None,
                },
            )

        if applied:
            _apply_to_project(session, source, definition, result.content, content_hash, new_hash)

        session.commit()
        session.refresh(drift)

        detail = (
            "Applied to the project." if applied else (withheld_reason or "Recorded for review.")
        )
        return CheckOutcome(
            status="changed",
            detail=detail,
            report_id=drift.id,
            applied=applied,
            breaking=breaking,
        )


def _apply_to_project(
    session: Session,
    source: ApiSource,
    definition: ApiDefinition,
    content: str,
    content_hash: str,
    ir_hash: str,
) -> None:
    """Take an accepted change into the project's own definition.

    Deliberately does **not** recompile or redeploy. The project's stored IR is
    updated; turning that into tools is a compile, and turning tools into a
    running runtime is a deployment — each with its own governance. Sync's job
    ends at "the definition is current".
    """
    project = session.get(OpenAPIProject, source.project_id)
    if project is None:
        return
    project.spec_text = content
    project.ir_json = definition.model_dump_json()
    project.ir_hash = ir_hash
    project.ir_version = definition.ir_version
    project.content_hash = content_hash
    project.warnings_json = json.dumps([w.model_dump() for w in definition.warnings])
    project.updated_at = datetime.utcnow()
    session.add(project)


def _fail(session: Session, source: ApiSource, message: str) -> CheckOutcome:
    source.consecutive_failures += 1
    source.last_error = message[:1000]
    source.last_checked_at = datetime.utcnow()
    if source.consecutive_failures >= FAILURE_PAUSE_THRESHOLD:
        source.watch_enabled = False
        source.last_error = (
            f"{message[:900]} — watching paused after "
            f"{source.consecutive_failures} consecutive failures."
        )
    session.add(source)
    session.commit()
    logger.warning("source %s check failed: %s", source.id, message)
    return CheckOutcome(status="failed", detail=message)


def compare_sources(session: Session, primary: ApiSource, other: ApiSource) -> DriftReport | None:
    """Report where two attached sources disagree with each other.

    The LLD calls this the governance differentiator (§3.3): a team whose
    source of truth is a repository while a gateway also serves a definition
    finds out that the two have diverged, rather than discovering it from a
    caller.
    """
    left = definition_of(primary)
    right = definition_of(other)
    if left is None or right is None:
        return None
    report = compare(left, right)
    counts = _counts(report)
    drift = DriftReport(
        org_id=primary.org_id,
        project_id=primary.project_id,
        source_id=other.id,
        compared_source_id=primary.id,
        origin=ORIGIN_COMPARISON,
        status=STATUS_OPEN,
        breaking_count=counts.get(Category.BREAKING.value, 0),
        non_breaking_count=counts.get(Category.NON_BREAKING.value, 0),
        security_count=counts.get(Category.SECURITY.value, 0),
        documentation_count=counts.get(Category.DOCUMENTATION.value, 0),
        metadata_count=counts.get(Category.METADATA.value, 0),
        changes_json=json.dumps([change.as_dict() for change in report.changes]),
        before_ir_hash=primary.ir_hash,
        after_ir_hash=other.ir_hash,
        withheld_reason=(
            "Comparison sources are never applied; they exist to report disagreement."
        ),
    )
    session.add(drift)
    return drift


def due_sources(session: Session, *, now: datetime | None = None) -> list[ApiSource]:
    """Watched sources whose next check is due, with failure backoff applied."""
    moment = now or datetime.utcnow()
    candidates = session.exec(select(ApiSource).where(col(ApiSource.watch_enabled).is_(True))).all()
    due = []
    for source in candidates:
        interval = source.watch_interval_seconds or 3600
        multiplier = min(2**source.consecutive_failures, MAX_BACKOFF_MULTIPLIER)
        wait = timedelta(seconds=interval * multiplier)
        if source.last_checked_at is None or source.last_checked_at + wait <= moment:
            due.append(source)
    return due


async def run_sync_sweep(limit: int = 25) -> dict[str, int]:
    """One pass over the sources that are due. Returns counts per outcome."""
    counts = {"checked": 0, "changed": 0, "unchanged": 0, "failed": 0}
    with Session(db.engine) as session:
        due = due_sources(session)[:limit]
        for source in due:
            outcome = await check_source(session, source, origin=ORIGIN_SYNC)
            counts["checked"] += 1
            if outcome.status in counts:
                counts[outcome.status] += 1
    return counts
