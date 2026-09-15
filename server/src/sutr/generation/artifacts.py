"""Storing runtime artifacts, and the determinism guarantee that shapes it.

LLD §3.6: *"Every stage produces immutable artifacts · identical inputs ⇒
identical outputs (deterministic builds)"*. Those two requirements are one
mechanism here.

`build_hash` is computed over the *inputs* (which project, which IR, which
documentation knowledge, which template) and the *output* (the digest of the
package bytes and of the manifest). Storage is keyed on it: generating again
from unchanged inputs finds the existing row and returns it rather than writing
a second one.

That makes the determinism claim self-enforcing instead of merely asserted. If
the packager ever became non-deterministic — an embedded timestamp, a dict
iterated in insertion order — the second build would produce a different
`package_sha256`, a different `build_hash`, and a second row. The test that
generates twice and asserts one artifact is the one that would catch it.

Nothing mutates an artifact after it is written. There is no update path, and
the API exposes none: an artifact that could be edited after validation would
make the validation gate meaningless, since the thing deployed would no longer
be the thing that passed.
"""

import hashlib
import json
import uuid
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.generation import signing
from sutr.models.runtime_artifact import (
    STATUS_REJECTED,
    STATUS_VALIDATED,
    RuntimeArtifact,
)

# Bumped when the *shape* of what is stored changes in a way that makes an
# older artifact non-comparable with a newer one — a new field folded into the
# build hash, a change in what the manifest covers. Not bumped for a template
# change: that is what `template_version` is for.
ARTIFACT_VERSION = "1"


def package_digest(package_zip: bytes) -> str:
    return hashlib.sha256(package_zip).hexdigest()


def build_hash(
    *,
    project_id: uuid.UUID | None,
    runtime: str,
    template_version: str,
    ir_version: int | None,
    ir_hash: str,
    knowledge_hash: str,
    manifest_hash: str,
    package_sha256: str,
) -> str:
    """The identity of one build.

    Includes the project id: two projects in the same tenant can hold the same
    specification, and they are still two providers' artifacts. Excludes the
    validation report and everything else measured at build time — a rebuild
    that took longer, or ran on an install with a scanner configured, is the
    same build.
    """
    payload = {
        "artifact_version": ARTIFACT_VERSION,
        "project_id": str(project_id) if project_id else "",
        "runtime": runtime,
        "template_version": template_version,
        "ir_version": ir_version,
        "ir_hash": ir_hash,
        "knowledge_hash": knowledge_hash,
        "manifest_hash": manifest_hash,
        "package_sha256": package_sha256,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find_by_build_hash(
    session: Session, *, org_id: uuid.UUID, value: str
) -> RuntimeArtifact | None:
    return session.exec(
        select(RuntimeArtifact).where(
            RuntimeArtifact.org_id == org_id, RuntimeArtifact.build_hash == value
        )
    ).first()


def get(session: Session, artifact_id: uuid.UUID, org_id: uuid.UUID) -> RuntimeArtifact | None:
    artifact = session.get(RuntimeArtifact, artifact_id)
    if artifact is None or artifact.org_id != org_id:
        # One answer for "no such artifact" and "another tenant's artifact".
        return None
    return artifact


def list_for_org(
    session: Session,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RuntimeArtifact]:
    statement = select(RuntimeArtifact).where(RuntimeArtifact.org_id == org_id)
    if project_id is not None:
        statement = statement.where(RuntimeArtifact.project_id == project_id)
    statement = statement.order_by(desc(col(RuntimeArtifact.created_at)))
    return list(session.exec(statement.offset(offset).limit(limit)).all())


def store(
    session: Session,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    name: str,
    slug: str,
    runtime: str,
    template_version: str,
    ir_version: int | None,
    ir_hash: str,
    knowledge_hash: str,
    knowledge: dict[str, Any],
    manifest: dict[str, Any],
    sbom: dict[str, Any],
    validation: dict[str, Any],
    package_zip: bytes,
    package_sha256: str,
    value: str,
    tool_count: int,
    created_by_user_id: uuid.UUID | None = None,
) -> RuntimeArtifact:
    """Write the artifact row. The caller commits."""
    signature = signing.sign(value)
    artifact = RuntimeArtifact(
        org_id=org_id,
        project_id=project_id,
        name=name,
        slug=slug,
        runtime=runtime,
        template_version=template_version,
        ir_version=ir_version,
        ir_hash=ir_hash,
        knowledge_hash=knowledge_hash,
        build_hash=value,
        package_zip=package_zip,
        package_sha256=package_sha256,
        package_bytes=len(package_zip),
        tool_count=tool_count,
        manifest_json=json.dumps(manifest, sort_keys=True),
        sbom_json=json.dumps(sbom, sort_keys=True),
        validation_json=json.dumps(validation),
        knowledge_json=json.dumps(knowledge, sort_keys=True),
        status=STATUS_VALIDATED if validation.get("validated") else STATUS_REJECTED,
        signature=signature.value if signature else "",
        signature_algorithm=signature.algorithm if signature else "",
        signature_key_id=signature.key_id if signature else "",
        created_by_user_id=created_by_user_id,
    )
    session.add(artifact)
    session.flush()
    return artifact


def deployable(artifact: RuntimeArtifact) -> tuple[bool, str | None]:
    """The LLD's rule: only validated artifacts are deployable."""
    if artifact.status == STATUS_VALIDATED:
        return True, None
    report = json.loads(artifact.validation_json or "{}")
    failed = ", ".join(report.get("failed") or []) or "validation"
    return False, (
        f"Artifact {artifact.id} did not pass validation ({failed}) and cannot be deployed. "
        "Fix the specification or the documentation it was generated from and generate again."
    )


def verify(artifact: RuntimeArtifact) -> dict[str, Any]:
    """Re-check an artifact against what was recorded about it.

    Two independent things, and they answer different questions. The digest
    says the stored bytes are the bytes that were validated. The signature says
    this platform, holding the signing key, vouched for that build hash. A
    reader is told both rather than one summary verdict.
    """
    recomputed = package_digest(artifact.package_zip)
    signature_state: dict[str, Any] = {"signed": bool(artifact.signature)}
    if artifact.signature:
        described = signing.describe()
        # Verify only against the key that made the signature. If the install's
        # key has since been rotated, say the key is unavailable rather than
        # reporting a failed verification — a rotated key is not tampering, and
        # reporting it as such would train readers to ignore the field.
        key = described.get("public_key") or ""
        if key and described.get("key_id") == artifact.signature_key_id:
            signature_state["verified"] = signing.verify(
                artifact.build_hash, artifact.signature, key
            )
        else:
            signature_state["verified"] = None
            signature_state["unavailable_reason"] = (
                "The key that signed this artifact is not the key this install now holds, "
                "so the signature cannot be checked here."
            )
        signature_state["key_id"] = artifact.signature_key_id
        signature_state["algorithm"] = artifact.signature_algorithm
    else:
        signature_state["verified"] = None
        signature_state["unavailable_reason"] = signing.NO_KEY
    return {
        "package_sha256": artifact.package_sha256,
        "package_sha256_recomputed": recomputed,
        "package_intact": recomputed == artifact.package_sha256,
        "signature": signature_state,
    }


def serialize(artifact: RuntimeArtifact, *, detail: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(artifact.id),
        "project_id": str(artifact.project_id) if artifact.project_id else None,
        "name": artifact.name,
        "slug": artifact.slug,
        "runtime": artifact.runtime,
        "template_version": artifact.template_version,
        "ir_version": artifact.ir_version,
        "ir_hash": artifact.ir_hash,
        "knowledge_hash": artifact.knowledge_hash or None,
        "build_hash": artifact.build_hash,
        "package_sha256": artifact.package_sha256,
        "package_bytes": artifact.package_bytes,
        "tool_count": artifact.tool_count,
        "status": artifact.status,
        "deployable": artifact.status == STATUS_VALIDATED,
        "signature": {
            "signed": bool(artifact.signature),
            "algorithm": artifact.signature_algorithm or None,
            "key_id": artifact.signature_key_id or None,
            "unavailable_reason": None if artifact.signature else signing.NO_KEY,
        },
        "created_at": artifact.created_at.isoformat(),
    }
    if detail:
        payload["manifest"] = json.loads(artifact.manifest_json or "{}")
        payload["validation"] = json.loads(artifact.validation_json or "{}")
        payload["knowledge"] = json.loads(artifact.knowledge_json or "{}")
        payload["verification"] = verify(artifact)
    return payload
