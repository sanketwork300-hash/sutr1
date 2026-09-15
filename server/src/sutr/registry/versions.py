"""Creating immutable versions from what the generator built.

LLD §3.7: *"every version immutable (v1→v2→v3…) with build hash, SBOM,
deployment manifest · old versions kept for rollback"*.

A version is created from a **validated runtime artifact**. That is the same
gate the deployment path applies, for the same reason: a version is a thing
somebody will roll back to, and rolling back to a build that never passed
validation would make the rollback the more dangerous operation.

The build hash, SBOM and deployment manifest are copied onto the version row
rather than read through the artifact. It costs a few kilobytes and it means a
version stays complete and rollback-able on its own — a version whose evidence
lives behind a foreign key is a version that a cascade delete can hollow out
without anybody noticing until the rollback.
"""

import json
import uuid

from sqlmodel import Session, col, desc, func, select

from sutr.common.errors import ConflictError, InvalidRequestError, NotFoundError
from sutr.models.registry_tool import RegistryTool
from sutr.models.registry_version import RegistryVersion
from sutr.models.runtime_artifact import STATUS_VALIDATED, RuntimeArtifact


def next_number(session: Session, tool_id: uuid.UUID) -> int:
    highest = session.exec(
        select(func.max(RegistryVersion.version)).where(RegistryVersion.tool_id == tool_id)
    ).one()
    return int(highest or 0) + 1


def list_for_tool(session: Session, tool_id: uuid.UUID) -> list[RegistryVersion]:
    return list(
        session.exec(
            select(RegistryVersion)
            .where(RegistryVersion.tool_id == tool_id)
            .order_by(desc(col(RegistryVersion.version)))
        ).all()
    )


def get(session: Session, tool_id: uuid.UUID, version: int) -> RegistryVersion | None:
    return session.exec(
        select(RegistryVersion)
        .where(RegistryVersion.tool_id == tool_id)
        .where(RegistryVersion.version == version)
    ).first()


def deployment_manifest(artifact: RuntimeArtifact | None, tool: RegistryTool) -> dict:
    """What a runtime manager needs to deploy this version.

    Assembled from the artifact's own manifest rather than invented: the
    environment variables, transports and endpoints are what the generator
    recorded, and restating them differently here would create a second answer
    to the same question.
    """
    manifest = json.loads(artifact.manifest_json) if artifact else {}
    return {
        "tool_key": tool.tool_key,
        "runtime": manifest.get("runtime"),
        "template_version": manifest.get("template_version"),
        "artifact_id": str(artifact.id) if artifact else None,
        "package_sha256": artifact.package_sha256 if artifact else None,
        "transports": manifest.get("transports", []),
        "endpoints": manifest.get("endpoints", {}),
        "environment": manifest.get("environment", []),
        "tool_count": manifest.get("tool_count", 0),
    }


def create(
    session: Session,
    *,
    tool: RegistryTool,
    artifact: RuntimeArtifact | None = None,
    notes: str = "",
    created_by_user_id: uuid.UUID | None = None,
) -> RegistryVersion:
    """Cut the next version. The caller commits."""
    if artifact is not None:
        if artifact.org_id != tool.org_id:
            # Reached only by a caller that skipped the org-scoped lookup;
            # refused here too, because a version is the thing that gets
            # deployed and one built from another tenant's artifact would be a
            # cross-tenant leak with a version number on it.
            raise NotFoundError("Runtime artifact not found.")
        if artifact.status != STATUS_VALIDATED:
            raise ConflictError(
                f"Artifact {artifact.id} did not pass validation, so it cannot become a version. "
                "A version is what a rollback restores; restoring an unvalidated build would "
                "make rollback the riskier operation."
            )
        if existing_for_artifact(session, tool.id, artifact.id) is not None:
            raise ConflictError(
                "That artifact is already a version of this tool. Generate a new artifact, or "
                "publish the version that exists."
            )
    elif not notes.strip():
        raise InvalidRequestError(
            "A version with no artifact needs notes saying what it is, or there is nothing to "
            "tell one version from another."
        )

    version = RegistryVersion(
        tool_id=tool.id,
        org_id=tool.org_id,
        version=next_number(session, tool.id),
        artifact_id=artifact.id if artifact else None,
        build_hash=artifact.build_hash if artifact else "",
        sbom_json=artifact.sbom_json if artifact else "{}",
        deployment_manifest_json=json.dumps(deployment_manifest(artifact, tool), sort_keys=True),
        tool_count=artifact.tool_count if artifact else 0,
        notes=notes,
        created_by_user_id=created_by_user_id,
    )
    session.add(version)
    session.flush()
    tool.current_version = version.version
    session.add(tool)
    return version


def existing_for_artifact(
    session: Session, tool_id: uuid.UUID, artifact_id: uuid.UUID
) -> RegistryVersion | None:
    return session.exec(
        select(RegistryVersion)
        .where(RegistryVersion.tool_id == tool_id)
        .where(RegistryVersion.artifact_id == artifact_id)
    ).first()


def serialize(version: RegistryVersion, *, detail: bool = False) -> dict:
    payload = {
        "version": version.version,
        "id": str(version.id),
        "artifact_id": str(version.artifact_id) if version.artifact_id else None,
        "build_hash": version.build_hash or None,
        "tool_count": version.tool_count,
        "notes": version.notes,
        "was_published": version.was_published,
        "created_at": version.created_at.isoformat(),
    }
    if detail:
        payload["sbom"] = json.loads(version.sbom_json or "{}")
        payload["deployment_manifest"] = json.loads(version.deployment_manifest_json or "{}")
    return payload
