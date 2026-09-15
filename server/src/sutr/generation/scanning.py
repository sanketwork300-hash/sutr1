"""Scans run over a generated package before it is allowed to be deployed.

LLD §3.6 asks for *static analysis, security scan* and *vulnerability scans*.
Three of those are done here directly, and the rest are honest about needing
something this install does not ship:

- **Secret detection** (implemented). Generated code must never contain a
  credential — build prompt §42 — and this is the check that makes that a fact
  rather than an intention. It reads the package's own files, so a template
  change that started interpolating a token into source would fail the build
  that introduced it.
- **Dependency policy** (implemented). Every requirement must name a package
  from an index and carry a version constraint. A dependency installed from a
  URL, a VCS ref or a local path is a supply chain nobody can audit, and an
  unconstrained one installs whatever the index happens to hold that morning.
- **Dangerous constructs** (implemented). An AST walk over the generated Python
  looking for `eval`, `exec`, `os.system`, `subprocess(..., shell=True)`,
  `pickle.loads` and friends. This is a real static analysis of a small,
  known-shaped body of code — not a general-purpose SAST tool, and it does not
  claim to be one.
- **Vulnerability and security scanning** (BLOCKED by default). A CVE scan
  needs a vulnerability database, and there is none in this repository and no
  network call this code is permitted to invent (build prompt §4). Instead an
  operator may name a command — theirs, whichever scanner they trust — and it
  is run against the unpacked package with a non-zero exit failing the gate.
  Unset, the check reports `blocked` with that reason rather than passing.
"""

import ast
import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

OK = "ok"
FAILED = "failed"
BLOCKED = "blocked"
SKIPPED = "skipped"

# Only the extension matters: everything in a generated package is text.
_PYTHON_SUFFIX = ".py"

# Patterns for credentials that have a recognisable shape. Deliberately not an
# entropy heuristic: on a file of generated code an entropy scan flags hashes,
# base64 examples and long identifiers, and a check that cries wolf gets
# switched off.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_secret_key", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("json_web_token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.")),
    (
        "assigned_credential",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*"""
            r"""["'][A-Za-z0-9_\-./+=]{16,}["']"""
        ),
    ),
)

# Names that execute or deserialize whatever they are handed.
_DANGEROUS_CALLS = {
    "eval": "evaluates arbitrary Python",
    "exec": "executes arbitrary Python",
    "compile": "compiles arbitrary Python",
    "__import__": "imports a module chosen at runtime",
}
_DANGEROUS_ATTRIBUTES = {
    ("os", "system"): "runs a shell command",
    ("os", "popen"): "runs a shell command",
    ("pickle", "loads"): "deserializes arbitrary objects",
    ("pickle", "load"): "deserializes arbitrary objects",
    ("marshal", "loads"): "deserializes arbitrary objects",
    ("yaml", "load"): "deserializes arbitrary objects unless SafeLoader is given",
}
_SUBPROCESS_FUNCTIONS = {"run", "call", "check_call", "check_output", "Popen"}

_URL_MARKERS = ("://", "git+", "file:", "@ ")
_CONSTRAINT_MARKERS = (">=", "==", "~=", "<=", "<", ">", "!=")


@dataclass
class Finding:
    """One thing a scan objected to, with enough detail to go and look."""

    code: str
    file: str
    line: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "file": self.file, "line": self.line, "detail": self.detail}


@dataclass
class ScanResult:
    name: str
    status: str
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "findings": [finding.as_dict() for finding in self.findings],
            "detail": self.detail,
        }


def scan_for_secrets(files: dict[str, str]) -> ScanResult:
    """Look for credentials in the generated package."""
    findings: list[Finding] = []
    for path in sorted(files):
        for number, line in enumerate(files[path].splitlines(), start=1):
            for code, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            code=code,
                            file=path,
                            line=number,
                            # The matching text is deliberately not repeated: a
                            # report that quotes the secret it found has copied
                            # it somewhere new.
                            detail=f"A value shaped like a {code.replace('_', ' ')} appears here.",
                        )
                    )
    return ScanResult(
        name="secrets",
        status=FAILED if findings else OK,
        summary=(
            f"{len(findings)} possible credential(s) in the generated package."
            if findings
            else "No credential-shaped values in the generated package."
        ),
        findings=findings,
    )


def scan_dependencies(files: dict[str, str]) -> ScanResult:
    """Check every declared requirement is auditable."""
    from sutr.generation.sbom import parse_requirements

    text = files.get("requirements.txt", "")
    findings: list[Finding] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if any(marker in line for marker in _URL_MARKERS) or line.startswith("-"):
            findings.append(
                Finding(
                    code="unauditable_source",
                    file="requirements.txt",
                    line=number,
                    detail=(
                        "Dependencies must come from a package index, not a URL, VCS ref or path."
                    ),
                )
            )
            continue
        if not any(marker in line for marker in _CONSTRAINT_MARKERS):
            findings.append(
                Finding(
                    code="unconstrained_version",
                    file="requirements.txt",
                    line=number,
                    detail="Declares no version constraint, so the build is not reproducible.",
                )
            )
    declared = parse_requirements(text)
    return ScanResult(
        name="dependencies",
        status=FAILED if findings else OK,
        summary=(
            f"{len(findings)} dependency problem(s)."
            if findings
            else f"{len(declared)} dependencies, all constrained and index-sourced."
        ),
        findings=findings,
        detail={"declared": [entry["name"] for entry in declared]},
    )


def _call_name(node: ast.Call) -> tuple[str, str] | None:
    func = node.func
    if isinstance(func, ast.Name):
        return ("", func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def scan_static(files: dict[str, str]) -> ScanResult:
    """Walk the generated Python for constructs that execute or deserialize."""
    findings: list[Finding] = []
    for path in sorted(files):
        if not path.endswith(_PYTHON_SUFFIX):
            continue
        try:
            tree = ast.parse(files[path], filename=path)
        except SyntaxError as exc:
            findings.append(
                Finding(
                    code="syntax_error",
                    file=path,
                    line=exc.lineno or 0,
                    detail=f"Could not be parsed: {exc.msg}",
                )
            )
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name is None:
                continue
            module, attribute = name
            if not module and attribute in _DANGEROUS_CALLS:
                findings.append(
                    Finding(
                        code="dangerous_call",
                        file=path,
                        line=node.lineno,
                        detail=f"`{attribute}` {_DANGEROUS_CALLS[attribute]}.",
                    )
                )
            elif (module, attribute) in _DANGEROUS_ATTRIBUTES:
                findings.append(
                    Finding(
                        code="dangerous_call",
                        file=path,
                        line=node.lineno,
                        detail=(
                            f"`{module}.{attribute}` {_DANGEROUS_ATTRIBUTES[(module, attribute)]}."
                        ),
                    )
                )
            elif module == "subprocess" and attribute in _SUBPROCESS_FUNCTIONS:
                shell = any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                )
                if shell:
                    findings.append(
                        Finding(
                            code="shell_injection_risk",
                            file=path,
                            line=node.lineno,
                            detail=f"`subprocess.{attribute}` with shell=True.",
                        )
                    )
    scanned = [path for path in files if path.endswith(_PYTHON_SUFFIX)]
    return ScanResult(
        name="static_analysis",
        status=FAILED if findings else OK,
        summary=(
            f"{len(findings)} dangerous construct(s) in generated code."
            if findings
            else f"{len(scanned)} generated Python files, no dangerous constructs."
        ),
        findings=findings,
        detail={"files_scanned": len(scanned)},
    )


async def run_external_scan(
    name: str, command: str, workdir: str, *, timeout_seconds: float = 300.0
) -> ScanResult:
    """Run an operator-supplied scanner over the unpacked package.

    The contract is the one every command-line tool already implements: run in
    this directory, exit zero if it is clean. Nothing is parsed out of the
    output, because parsing it would mean guessing at the format of a tool
    whose identity is the operator's choice — and guessing at a tool's output
    schema is exactly what build prompt §4 forbids. The output is captured and
    stored so a human can read what the scanner said.
    """
    if not command.strip():
        return ScanResult(
            name=name,
            status=BLOCKED,
            summary=(
                f"NOT_CONFIGURED: no {name.replace('_', ' ')} command is configured, so this "
                "artifact has not been scanned. Set the corresponding setting to a command that "
                "exits non-zero on a finding."
            ),
        )
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return ScanResult(
            name=name, status=FAILED, summary=f"The {name} command could not be started: {exc}"
        )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return ScanResult(
            name=name,
            status=FAILED,
            summary=f"The {name} command did not finish within {timeout_seconds:.0f}s.",
        )
    output = (stdout or b"").decode("utf-8", errors="replace")
    return ScanResult(
        name=name,
        status=OK if process.returncode == 0 else FAILED,
        summary=(
            f"The {name} command exited {process.returncode}."
            if process.returncode
            else f"The {name} command reported no findings."
        ),
        # Bounded: a scanner report is evidence, not a log store.
        detail={"exit_code": process.returncode, "output": output[-8000:]},
    )
