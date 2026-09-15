"""The generation pipeline, stage by stage.

    knowledge → metadata → generate → validate → store

Each stage records what it did, how long it took and what it produced, for the
same reason the translation pipeline does (`openapi/pipeline.py`): when a build
comes out wrong, "which stage" and "why" is the whole question, and one error
raised from somewhere in the middle answers neither.

Two behaviours are worth stating because they are not the obvious ones.

**A rebuild of unchanged inputs is not a new artifact.** The build hash is
looked up before validation; a hit returns the stored artifact, republishes
`mcp.generated` with `reused: true`, and does not re-run the gate. Re-running
validation would be re-validating bytes that already passed, and storing a
second row would quietly contradict the determinism the artifact record exists
to demonstrate.

**A rejected artifact is still stored.** Validation failing is not the pipeline
failing: the build happened, the report is the finding, and throwing it away
would leave the provider with an error message and no way to see which check
objected. It is stored with `status = rejected`, and the deployment path
refuses it.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from sutr.documentation.linking import LinkReport, link_workflows
from sutr.generation import artifacts, events
from sutr.generation import knowledge as knowledge_module
from sutr.generation import manifest as manifest_module
from sutr.generation import sbom as sbom_module
from sutr.generation import validation as validation_module
from sutr.generation.knowledge import Knowledge
from sutr.models.runtime_artifact import RUNTIME_PYTHON, RuntimeArtifact
from sutr.openapi import packaging

OK = "ok"
FAILED = "failed"
REUSED = "reused"

# The runtimes the LLD names (§3.6: Python, Go, Node.js; Java future). Only
# Python has a template. The others are declared here so a request for one gets
# "not implemented" naming the runtime, rather than a generic 400 that leaves a
# caller guessing whether they misspelled it.
SUPPORTED_RUNTIMES = (RUNTIME_PYTHON,)
DECLARED_RUNTIMES = (RUNTIME_PYTHON, "go", "node")

GENERATOR_VERSION = "1"


class GenerationError(Exception):
    """A stage failed. `stage` and `code` say which and why."""

    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message


@dataclass
class StageResult:
    name: str
    status: str
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


@dataclass
class GenerationResult:
    generation_id: uuid.UUID
    artifact: RuntimeArtifact
    reused: bool
    stages: list[StageResult]
    knowledge: Knowledge
    links: LinkReport

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_id": str(self.generation_id),
            "reused": self.reused,
            "stages": [stage.as_dict() for stage in self.stages],
            "knowledge": self.knowledge.summary(),
            "workflow_links": self.links.as_dict(),
            "artifact": artifacts.serialize(self.artifact, detail=True),
        }


def runtime_support(runtime: str) -> tuple[bool, str | None]:
    if runtime in SUPPORTED_RUNTIMES:
        return True, None
    if runtime in DECLARED_RUNTIMES:
        return False, (
            f"NOT IMPLEMENTED: the '{runtime}' runtime template does not exist in this build. "
            f"The LLD names Python, Go and Node.js templates; only "
            f"{', '.join(SUPPORTED_RUNTIMES)} is implemented."
        )
    return False, f"Unknown runtime '{runtime}'. Known runtimes: {', '.join(DECLARED_RUNTIMES)}."


async def generate(
    session: Session,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    name: str,
    base_url: str,
    token_header: str,
    token_format: str,
    tools: list[Any],
    api_title: str,
    api_version: str,
    ir_version: int | None,
    ir_hash: str,
    runtime: str = RUNTIME_PYTHON,
    created_by_user_id: uuid.UUID | None = None,
) -> GenerationResult:
    """Run a generation. Publishes the LLD §3.6 event chain as it goes.

    The caller commits: everything written here — the artifact, the workflow
    links, every event — is in the caller's transaction, so a failure after a
    partial write leaves nothing behind.
    """
    supported, reason = runtime_support(runtime)
    if not supported:
        raise GenerationError("metadata", "runtime_not_supported", reason or "")
    if not tools:
        raise GenerationError(
            "metadata",
            "no_tools",
            "The project compiles to no tools, so there is nothing to generate a server for.",
        )

    generation_id = uuid.uuid4()
    stages: list[StageResult] = []

    # ── Knowledge ────────────────────────────────────────────────────────────
    started = time.monotonic()
    links = link_workflows(session, org_id=org_id, project_id=project_id, tools=tools)
    collected = knowledge_module.collect(session, org_id=org_id, project_id=project_id, tools=tools)
    stages.append(
        StageResult(
            name="knowledge",
            status=OK,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail={**collected.summary(), "workflow_links": links.linked_steps},
        )
    )

    # ── Metadata ─────────────────────────────────────────────────────────────
    events.metadata_generated(
        session,
        org_id=org_id,
        generation_id=generation_id,
        project_id=project_id,
        tool_count=len(tools),
        knowledge=collected.summary(),
    )
    events.started(
        session,
        org_id=org_id,
        generation_id=generation_id,
        project_id=project_id,
        runtime=runtime,
        template_version=packaging.TEMPLATE_VERSION,
    )

    # ── Generate ─────────────────────────────────────────────────────────────
    started = time.monotonic()
    try:
        files = packaging.build_server_files(
            name=name,
            base_url=base_url,
            token_header=token_header,
            token_format=token_format,
            tools=tools,
            api_title=api_title,
            api_version=api_version,
            knowledge=collected.as_dict() if not collected.empty else None,
        )
    except ValueError as exc:
        events.failed(
            session,
            org_id=org_id,
            generation_id=generation_id,
            stage="generate",
            error_code="packaging_failed",
            error_message=str(exc),
        )
        raise GenerationError("generate", "packaging_failed", str(exc))

    package_zip = packaging.zip_files(files)
    package_sha256 = artifacts.package_digest(package_zip)
    bundle = _bundle(files)
    knowledge_hash = collected.content_hash()
    manifest = manifest_module.build_manifest(
        files=files,
        bundle=bundle,
        runtime=runtime,
        template_version=packaging.TEMPLATE_VERSION,
        ir_version=ir_version,
        ir_hash=ir_hash,
        knowledge_hash=knowledge_hash,
        knowledge_summary=collected.summary(),
    )
    sbom = sbom_module.build_sbom(
        files=files,
        manifest=manifest,
        package_sha256=package_sha256,
        generator_version=GENERATOR_VERSION,
    )
    value = artifacts.build_hash(
        project_id=project_id,
        runtime=runtime,
        template_version=packaging.TEMPLATE_VERSION,
        ir_version=ir_version,
        ir_hash=ir_hash,
        knowledge_hash=knowledge_hash,
        manifest_hash=manifest_module.manifest_hash(manifest),
        package_sha256=package_sha256,
    )
    stages.append(
        StageResult(
            name="generate",
            status=OK,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail={
                "files": len(files),
                "package_bytes": len(package_zip),
                "build_hash": value,
                "sbom_components": sbom_module.component_counts(sbom),
            },
        )
    )

    existing = artifacts.find_by_build_hash(session, org_id=org_id, value=value)
    if existing is not None:
        stages.append(
            StageResult(
                name="validate",
                status=REUSED,
                detail={"artifact_id": str(existing.id), "status": existing.status},
            )
        )
        _announce(session, org_id, generation_id, existing, reused=True)
        return GenerationResult(
            generation_id=generation_id,
            artifact=existing,
            reused=True,
            stages=stages,
            knowledge=collected,
            links=links,
        )

    # ── Validate ─────────────────────────────────────────────────────────────
    started = time.monotonic()
    report = await validation_module.validate(files, manifest)
    stages.append(
        StageResult(
            name="validate",
            status=OK if report.validated else FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail={
                "validated": report.validated,
                "failed": [check.name for check in report.failed],
                "blocked": [check.name for check in report.blocked],
            },
        )
    )

    # ── Store ────────────────────────────────────────────────────────────────
    started = time.monotonic()
    artifact = artifacts.store(
        session,
        org_id=org_id,
        project_id=project_id,
        name=name,
        slug=bundle.get("slug", ""),
        runtime=runtime,
        template_version=packaging.TEMPLATE_VERSION,
        ir_version=ir_version,
        ir_hash=ir_hash,
        knowledge_hash=knowledge_hash,
        knowledge=collected.as_dict(),
        manifest=manifest,
        sbom=sbom,
        validation=report.as_dict(),
        package_zip=package_zip,
        package_sha256=package_sha256,
        value=value,
        tool_count=len(tools),
        created_by_user_id=created_by_user_id,
    )
    stages.append(
        StageResult(
            name="store",
            status=OK,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail={"artifact_id": str(artifact.id), "status": artifact.status},
        )
    )
    _announce(session, org_id, generation_id, artifact, reused=False)
    return GenerationResult(
        generation_id=generation_id,
        artifact=artifact,
        reused=False,
        stages=stages,
        knowledge=collected,
        links=links,
    )


def _bundle(files: dict[str, str]) -> dict[str, Any]:
    return json.loads(files.get("tools.json", "{}"))


def _announce(
    session: Session,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    artifact: RuntimeArtifact,
    *,
    reused: bool,
) -> None:
    report = json.loads(artifact.validation_json or "{}")
    events.generated(
        session,
        org_id=org_id,
        generation_id=generation_id,
        artifact_id=artifact.id,
        build_hash=artifact.build_hash,
        package_sha256=artifact.package_sha256,
        tool_count=artifact.tool_count,
        signed=bool(artifact.signature),
        reused=reused,
    )
    events.validation_completed(
        session,
        org_id=org_id,
        generation_id=generation_id,
        artifact_id=artifact.id,
        validated=bool(report.get("validated")),
        failed_checks=list(report.get("failed") or []),
        blocked_checks=list(report.get("blocked") or []),
    )
