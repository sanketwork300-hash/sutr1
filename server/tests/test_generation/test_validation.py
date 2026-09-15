"""The gate: what passes, what is refused, and what is honestly not run."""

import json

from sutr.config import settings
from sutr.generation import validation
from sutr.generation.scanning import BLOCKED, FAILED, OK, SKIPPED

from .conftest import build_files, build_manifest


async def test_a_freshly_generated_package_passes_every_check_that_can_run():
    files = build_files()
    report = await validation.validate(files, build_manifest(files))
    assert report.validated
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["compile"] == OK
    assert statuses["mcp_compliance"] == OK
    assert statuses["dependencies"] == OK
    assert statuses["static_analysis"] == OK
    assert statuses["secrets"] == OK
    # No scanner is configured in the test environment, and the report says so
    # rather than reporting a pass.
    assert statuses["security_scan"] == BLOCKED
    assert statuses["vulnerability_scan"] == BLOCKED
    assert statuses["tests"] == SKIPPED


async def test_a_validated_report_still_names_what_was_not_run():
    files = build_files()
    report = await validation.validate(files, build_manifest(files))
    assert set(report.as_dict()["blocked"]) == {"security_scan", "vulnerability_scan"}
    assert "not run on this install" in report.summary()


async def test_code_that_does_not_compile_is_rejected():
    files = build_files()
    files["server.py"] = "def broken(:\n"
    report = await validation.validate(files, build_manifest(files))
    assert not report.validated
    assert "compile" in [check.name for check in report.failed]


async def test_a_tool_name_no_mcp_client_would_accept_is_rejected():
    files = build_files()
    bundle = json.loads(files["tools.json"])
    bundle["tools"][0]["name"] = "list pets!"
    files["tools.json"] = json.dumps(bundle, indent=2, sort_keys=True)
    report = await validation.validate(files, build_manifest(files))
    assert not report.validated
    compliance = next(check for check in report.checks if check.name == "mcp_compliance")
    assert compliance.status == FAILED
    assert any(f.code == "invalid_tool_name" for f in compliance.findings)


async def test_a_tool_without_a_description_is_rejected():
    files = build_files()
    bundle = json.loads(files["tools.json"])
    bundle["tools"][0]["description"] = "  "
    files["tools.json"] = json.dumps(bundle, indent=2, sort_keys=True)
    report = await validation.validate(files, build_manifest(files))
    compliance = next(check for check in report.checks if check.name == "mcp_compliance")
    assert any(f.code == "missing_description" for f in compliance.findings)


async def test_a_manifest_that_disagrees_with_the_bundle_is_rejected():
    files = build_files()
    manifest = build_manifest(files)
    manifest["tool_count"] = 99
    report = await validation.validate(files, manifest)
    compliance = next(check for check in report.checks if check.name == "mcp_compliance")
    assert any(f.code == "manifest_mismatch" for f in compliance.findings)


async def test_a_relative_base_url_is_rejected():
    files = build_files(base_url="/v1")
    report = await validation.validate(files, build_manifest(files))
    compliance = next(check for check in report.checks if check.name == "mcp_compliance")
    assert any(f.code == "invalid_base_url" for f in compliance.findings)


async def test_the_generated_packages_own_test_suite_runs_and_passes(monkeypatch):
    """The slow one, run once: the LLD's unit tests, on the real artifact."""
    monkeypatch.setattr(settings, "generation_run_generated_tests", True)
    files = build_files()
    report = await validation.validate(files, build_manifest(files))
    tests = next(check for check in report.checks if check.name == "tests")
    assert tests.status == OK, tests.detail.get("output", tests.summary)
    assert report.validated


async def test_a_broken_runtime_fails_the_generated_test_suite(monkeypatch):
    monkeypatch.setattr(settings, "generation_run_generated_tests", True)
    files = build_files()
    # Break request building without breaking the syntax, so this fails at the
    # tests rather than at the compile check.
    files["sutr_runtime.py"] = files["sutr_runtime.py"].replace(
        'def build_tool_request(bundle, tool, args, token=""):',
        "def build_tool_request(bundle, tool, args, token=\"\"):\n    raise RuntimeError('broken')",
    )
    report = await validation.validate(files, build_manifest(files))
    tests = next(check for check in report.checks if check.name == "tests")
    assert tests.status == FAILED
    assert not report.validated


def test_describe_reports_which_checks_this_install_can_actually_run():
    described = validation.describe()
    assert described["compile"]["available"] is True
    assert described["security_scan"]["available"] is False
    assert "NOT_CONFIGURED" in described["security_scan"]["unavailable_reason"]
