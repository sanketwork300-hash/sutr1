"""What the scans catch, and what they honestly refuse to claim."""

import pytest

from sutr.config import settings
from sutr.generation import scanning

from .conftest import build_files


def test_the_generated_package_contains_no_credentials():
    """The check that makes build prompt §42 a fact rather than an intention."""
    result = scanning.scan_for_secrets(build_files())
    assert result.status == scanning.OK, [f.as_dict() for f in result.findings]


def test_a_planted_credential_is_found_and_not_echoed_back():
    files = build_files()
    files["server.py"] += '\nSECRET_TOKEN = "AKIAIOSFODNN7EXAMPLE"\n'
    result = scanning.scan_for_secrets(files)
    assert result.status == scanning.FAILED
    finding = next(f for f in result.findings if f.code == "aws_access_key_id")
    assert finding.file == "server.py"
    # A report that quoted the secret would have copied it somewhere new.
    assert "AKIAIOSFODNN7EXAMPLE" not in finding.detail


def test_an_assigned_api_key_literal_is_found():
    files = {"config.py": 'api_key = "abcd1234abcd1234abcd1234"\n'}
    result = scanning.scan_for_secrets(files)
    assert [f.code for f in result.findings] == ["assigned_credential"]


def test_the_generated_dependencies_are_constrained_and_index_sourced():
    result = scanning.scan_dependencies(build_files())
    assert result.status == scanning.OK
    assert set(result.detail["declared"]) >= {"mcp", "httpx", "pyjwt", "uvicorn"}


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("httpx @ https://example.com/httpx.whl", "unauditable_source"),
        ("git+https://example.com/pkg.git", "unauditable_source"),
        ("-e ./local", "unauditable_source"),
        ("httpx", "unconstrained_version"),
    ],
)
def test_an_unauditable_dependency_fails_the_check(line, code):
    result = scanning.scan_dependencies({"requirements.txt": line + "\n"})
    assert result.status == scanning.FAILED
    assert [f.code for f in result.findings] == [code]


def test_the_generated_code_contains_no_dangerous_constructs():
    result = scanning.scan_static(build_files())
    assert result.status == scanning.OK, [f.as_dict() for f in result.findings]
    assert result.detail["files_scanned"] >= 4


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("value = eval(payload)\n", "dangerous_call"),
        ("import os\nos.system(cmd)\n", "dangerous_call"),
        ("import pickle\npickle.loads(blob)\n", "dangerous_call"),
        ("import subprocess\nsubprocess.run(cmd, shell=True)\n", "shell_injection_risk"),
    ],
)
def test_static_analysis_finds_execution_and_deserialization(source, code):
    result = scanning.scan_static({"x.py": source})
    assert result.status == scanning.FAILED
    assert [f.code for f in result.findings] == [code]


def test_subprocess_without_a_shell_is_not_a_finding():
    result = scanning.scan_static({"x.py": "import subprocess\nsubprocess.run(['ls'])\n"})
    assert result.status == scanning.OK


async def test_an_unconfigured_scanner_is_blocked_rather_than_passing(tmp_path):
    """The distinction the whole report rests on: not run ≠ found nothing."""
    result = await scanning.run_external_scan("security_scan", "", str(tmp_path))
    assert result.status == scanning.BLOCKED
    assert result.status != scanning.OK
    assert "NOT_CONFIGURED" in result.summary


async def test_a_configured_scanner_runs_and_its_exit_code_decides(tmp_path):
    clean = await scanning.run_external_scan("security_scan", "true", str(tmp_path))
    assert clean.status == scanning.OK
    dirty = await scanning.run_external_scan("security_scan", "false", str(tmp_path))
    assert dirty.status == scanning.FAILED
    assert dirty.detail["exit_code"] == 1


async def test_a_scanner_that_hangs_fails_rather_than_blocking_the_build(tmp_path):
    result = await scanning.run_external_scan(
        "security_scan", "sleep 5", str(tmp_path), timeout_seconds=0.2
    )
    assert result.status == scanning.FAILED
    assert "did not finish" in result.summary


async def test_the_scanner_output_is_captured_for_a_human_to_read(tmp_path):
    result = await scanning.run_external_scan(
        "vulnerability_scan", "echo found-something; exit 3", str(tmp_path)
    )
    assert result.status == scanning.FAILED
    assert "found-something" in result.detail["output"]
    assert result.detail["exit_code"] == 3
    assert settings.generation_vulnerability_scan_command == ""
