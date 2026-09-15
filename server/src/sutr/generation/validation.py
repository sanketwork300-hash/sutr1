"""The validation gate: only a validated artifact may be deployed.

LLD §3.6 lists what has to pass before an artifact is deployable — *compile,
dependency check, MCP compliance, static analysis, security scan,
unit/integration/smoke tests* — and then states the consequence plainly: *only
validated artifacts deployable*. The consequence is the part that matters. A
validation report nobody enforces is a document; this one is a gate, checked in
`services/deployments.py` before a deployment is created.

Every check reports one of four states, and the difference between the last two
is the whole reason there are four:

- ``ok``       the check ran and found nothing.
- ``failed``   the check ran and found something. The artifact is rejected.
- ``blocked``  the check *could not run* because this install lacks what it
               needs — a scanner, a vulnerability database. Recorded, named,
               and not counted as a pass. The artifact is still deployable,
               because refusing every deployment on an install with no scanner
               would mean the platform never deploys anything; what it must not
               do is let a reader believe the scan happened.
- ``skipped``  the check does not apply to this artifact.

The report carries durations, which is why it is deliberately *not* part of the
artifact's build hash: a rebuild that took a different number of milliseconds
is the same build.
"""

import asyncio
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sutr.config import settings
from sutr.generation import scanning
from sutr.generation.scanning import BLOCKED, FAILED, OK, SKIPPED, Finding, ScanResult
from sutr.runtime.request_builder import tool_input_schema

# The strictest constraint that mainstream MCP clients enforce on a tool name.
# Generating a name they will reject produces a server that starts, advertises
# its tools, and is then unusable — a failure worth catching at build time.
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}

_TEST_TIMEOUT_SECONDS = 180.0


@dataclass
class Check:
    name: str
    status: str
    summary: str = ""
    duration_ms: int = 0
    findings: list[Finding] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "findings": [finding.as_dict() for finding in self.findings],
            "detail": self.detail,
        }


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAILED]

    @property
    def blocked(self) -> list[Check]:
        return [check for check in self.checks if check.status == BLOCKED]

    @property
    def validated(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "validated": self.validated,
            "checks": [check.as_dict() for check in self.checks],
            "failed": [check.name for check in self.failed],
            "blocked": [check.name for check in self.blocked],
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if self.failed:
            names = ", ".join(check.name for check in self.failed)
            return f"Rejected: {names} failed."
        if self.blocked:
            names = ", ".join(check.name for check in self.blocked)
            return f"Validated, with {names} not run on this install."
        return "Validated: every check ran and passed."


def _timed(scan: ScanResult, started: float) -> Check:
    return Check(
        name=scan.name,
        status=scan.status,
        summary=scan.summary,
        duration_ms=int((time.monotonic() - started) * 1000),
        findings=scan.findings,
        detail=scan.detail,
    )


def check_compiles(files: dict[str, str]) -> Check:
    """Every generated Python file compiles under this interpreter."""
    started = time.monotonic()
    findings: list[Finding] = []
    sources = sorted(path for path in files if path.endswith(".py"))
    for path in sources:
        try:
            compile(files[path], path, "exec")
        except SyntaxError as exc:
            findings.append(
                Finding(
                    code="syntax_error",
                    file=path,
                    line=exc.lineno or 0,
                    detail=exc.msg or "Failed to compile.",
                )
            )
    return Check(
        name="compile",
        status=FAILED if findings else OK,
        summary=(
            f"{len(findings)} file(s) failed to compile."
            if findings
            else f"{len(sources)} generated Python files compile."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
        findings=findings,
        detail={"files": len(sources)},
    )


def _schema_findings(name: str, schema: dict) -> list[Finding]:
    findings: list[Finding] = []
    where = "tools.json"
    if schema.get("type") != "object":
        findings.append(
            Finding("schema_not_object", where, 0, f"{name}: inputSchema must be type 'object'.")
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        findings.append(
            Finding("schema_no_properties", where, 0, f"{name}: inputSchema has no properties.")
        )
        return findings
    for field_name, declared in properties.items():
        if not isinstance(declared, dict):
            findings.append(
                Finding("schema_bad_property", where, 0, f"{name}.{field_name}: not an object.")
            )
            continue
        kind = declared.get("type")
        if kind is not None and kind not in _JSON_TYPES:
            findings.append(
                Finding(
                    "schema_unknown_type",
                    where,
                    0,
                    f"{name}.{field_name}: '{kind}' is not a JSON Schema type.",
                )
            )
    for required in schema.get("required") or []:
        if required not in properties:
            findings.append(
                Finding(
                    "schema_required_missing",
                    where,
                    0,
                    f"{name}: '{required}' is required but not declared.",
                )
            )
    return findings


def check_mcp_compliance(files: dict[str, str], manifest: dict) -> Check:
    """The tool set is something an MCP client will accept."""
    started = time.monotonic()
    findings: list[Finding] = []
    try:
        bundle = json.loads(files.get("tools.json", "{}"))
    except json.JSONDecodeError as exc:
        return Check(
            name="mcp_compliance",
            status=FAILED,
            summary="tools.json is not valid JSON.",
            duration_ms=int((time.monotonic() - started) * 1000),
            findings=[Finding("invalid_bundle", "tools.json", exc.lineno, exc.msg)],
        )

    tools = bundle.get("tools") or []
    if not tools:
        findings.append(Finding("no_tools", "tools.json", 0, "The bundle declares no tools."))

    seen: set[str] = set()
    for tool in tools:
        name = tool.get("name", "")
        if not TOOL_NAME.match(name or ""):
            findings.append(
                Finding(
                    "invalid_tool_name",
                    "tools.json",
                    0,
                    f"'{name}' is not a usable MCP tool name (letters, digits, _ and -, ≤64).",
                )
            )
        if name in seen:
            findings.append(
                Finding("duplicate_tool_name", "tools.json", 0, f"'{name}' is declared twice.")
            )
        seen.add(name)
        if not (tool.get("description") or "").strip():
            findings.append(
                Finding(
                    "missing_description",
                    "tools.json",
                    0,
                    f"'{name}' has no description, so an agent cannot tell when to call it.",
                )
            )
        findings.extend(_schema_findings(name or "?", tool_input_schema(tool)))

    base_url = bundle.get("base_url") or ""
    if not base_url.startswith(("http://", "https://")):
        findings.append(
            Finding("invalid_base_url", "tools.json", 0, "base_url must be an absolute HTTP URL.")
        )
    if not manifest.get("transports"):
        findings.append(
            Finding("no_transports", "manifest", 0, "The manifest declares no transport.")
        )
    if manifest.get("tool_count") != len(tools):
        findings.append(
            Finding(
                "manifest_mismatch",
                "manifest",
                0,
                "The manifest's tool count disagrees with the bundle.",
            )
        )

    return Check(
        name="mcp_compliance",
        status=FAILED if findings else OK,
        summary=(
            f"{len(findings)} compliance problem(s)."
            if findings
            else f"{len(tools)} tools, all with usable names, descriptions and schemas."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
        findings=findings,
    )


async def run_generated_tests(workdir: Path) -> Check:
    """Run the package's own offline test suite.

    The generated package ships `test_server.py`, which exercises schema
    generation and request building for every tool without a network. Running
    it here is what turns "the code compiles" into "the code does the thing" —
    the LLD's unit/integration/smoke tests, at the level a generated artifact
    can be tested offline.

    Integration and smoke tests against the *provider's live API* are a
    different thing and are not run: they would mean calling somebody else's
    production API with a real credential as a side effect of a build.
    """
    started = time.monotonic()
    if not settings.generation_run_generated_tests:
        return Check(
            name="tests",
            status=SKIPPED,
            summary="Disabled by configuration (GENERATION_RUN_GENERATED_TESTS=false).",
        )
    try:
        import pytest  # noqa: F401
    except ImportError:
        return Check(
            name="tests",
            status=BLOCKED,
            summary=(
                "NOT_CONFIGURED: pytest is not importable in this environment, so the generated "
                "package's own test suite was not run."
            ),
        )
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "test_server.py",
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return Check(name="tests", status=BLOCKED, summary=f"pytest could not be started: {exc}")
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=_TEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return Check(
            name="tests",
            status=FAILED,
            summary=f"The generated test suite did not finish within {_TEST_TIMEOUT_SECONDS:.0f}s.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    output = (stdout or b"").decode("utf-8", errors="replace")
    passed = process.returncode == 0
    return Check(
        name="tests",
        status=OK if passed else FAILED,
        summary=(
            "The generated package's own test suite passed."
            if passed
            else f"The generated test suite exited {process.returncode}."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
        detail={"exit_code": process.returncode, "output": output[-8000:]},
    )


async def validate(files: dict[str, str], manifest: dict) -> ValidationReport:
    """Run every check against a generated package.

    The package is written to a temporary directory once and reused by the
    checks that need a filesystem — the test run and any operator-supplied
    scanner — so a scanner sees exactly the files the artifact contains rather
    than a re-render of them.
    """
    checks = [check_compiles(files), check_mcp_compliance(files, manifest)]

    started = time.monotonic()
    checks.append(_timed(scanning.scan_dependencies(files), started))
    started = time.monotonic()
    checks.append(_timed(scanning.scan_static(files), started))
    started = time.monotonic()
    checks.append(_timed(scanning.scan_for_secrets(files), started))

    with tempfile.TemporaryDirectory(prefix="sutr-validate-") as directory:
        workdir = Path(directory)
        for path in sorted(files):
            target = workdir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[path], encoding="utf-8")

        started = time.monotonic()
        checks.append(
            _timed(
                await scanning.run_external_scan(
                    "security_scan", settings.generation_security_scan_command, str(workdir)
                ),
                started,
            )
        )
        started = time.monotonic()
        checks.append(
            _timed(
                await scanning.run_external_scan(
                    "vulnerability_scan",
                    settings.generation_vulnerability_scan_command,
                    str(workdir),
                ),
                started,
            )
        )
        checks.append(await run_generated_tests(workdir))

    return ValidationReport(checks=checks)


def describe() -> dict[str, Any]:
    """What this install can actually check, for `GET /capabilities`."""
    security_configured = bool(settings.generation_security_scan_command.strip())
    vulnerability_configured = bool(settings.generation_vulnerability_scan_command.strip())
    try:
        import pytest  # noqa: F401

        tests_available = True
        tests_reason = None
    except ImportError:
        tests_available = False
        tests_reason = "pytest is not importable in this environment."
    return {
        "compile": {"available": True, "unavailable_reason": None},
        "mcp_compliance": {"available": True, "unavailable_reason": None},
        "dependencies": {"available": True, "unavailable_reason": None},
        "static_analysis": {
            "available": True,
            "unavailable_reason": None,
            "detail": "AST scan for dangerous constructs; not a general-purpose SAST tool.",
        },
        "secrets": {"available": True, "unavailable_reason": None},
        "security_scan": {
            "available": security_configured,
            "unavailable_reason": (
                None
                if security_configured
                else "NOT_CONFIGURED: GENERATION_SECURITY_SCAN_COMMAND is unset."
            ),
        },
        "vulnerability_scan": {
            "available": vulnerability_configured,
            "unavailable_reason": (
                None
                if vulnerability_configured
                else "NOT_CONFIGURED: GENERATION_VULNERABILITY_SCAN_COMMAND is unset."
            ),
        },
        "tests": {
            "available": tests_available and settings.generation_run_generated_tests,
            "unavailable_reason": tests_reason,
        },
    }
