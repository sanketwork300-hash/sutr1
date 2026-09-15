"""The load and chaos assets, and the security-gate record.

A directory of scripts nobody runs proves nothing, and a document claiming a
number nobody measured is worse than no document. These tests keep three
promises honest: that the chaos scenarios say which ones were actually run,
that the load results refuse to claim the LLD's targets, and that every
Semgrep suppression in the tree is explained in `docs/SECURITY_GATES.md`.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
QA = REPO / "qa"


def test_every_chaos_scenario_declares_whether_it_was_run():
    """A scenario that has never been executed proves nothing about the system,
    however carefully its script is written."""
    readme = (QA / "chaos" / "README.md").read_text()
    rows = [line for line in readme.splitlines() if line.startswith("| ") and "|" in line[2:]]
    scenarios = [row for row in rows if re.match(r"\|\s*\d+\s*\|", row)]

    assert len(scenarios) == 7, "LLD §5.8 names seven scenarios"
    for row in scenarios:
        assert any(status in row for status in ("**RUN**", "**PARTIAL**", "**NOT RUN**")), (
            f"scenario without a status: {row}"
        )


def test_the_scripts_the_chaos_readme_offers_exist():
    for script in ("provider_failure.sh", "bus_outage.py"):
        assert (QA / "chaos" / script).exists(), f"{script} is offered and missing"


def test_the_load_results_name_the_machine_and_refuse_the_targets():
    """A number without the machine it was taken on is not a result."""
    results = (QA / "load" / "RESULTS.md").read_text()

    assert "NOT TESTED" in results
    assert "100 000" in results or "100,000" in results  # the target it declines to claim
    # And what it ran on, without which the number means nothing.
    assert "SQLite" in results
    assert "cores" in results


def test_the_load_script_thresholds_are_tripwires_not_slos():
    """An SLO this repository has not measured is a number it must not
    publish, so the thresholds say what they are."""
    script = (QA / "load" / "smoke.js").read_text()
    assert "regression tripwires" in script
    assert "NOT" in (QA / "load" / "README.md").read_text()


def test_every_semgrep_suppression_is_explained_in_the_record():
    """Fix by preference, accept by exception — and an exception is recorded
    twice, at the site and in the document, so neither can drift alone."""
    record = (REPO / "docs" / "SECURITY_GATES.md").read_text()

    suppressed: set[str] = set()
    for path in (
        list(REPO.glob("**/*.py"))
        + list(REPO.glob("**/*.js"))
        + [
            REPO / "dev.Dockerfile",
            REPO / "ui" / "index.html",
        ]
    ):
        if not path.is_file() or "node_modules" in path.parts or ".venv" in path.parts:
            continue
        for match in re.finditer(r"nosemgrep:\s*([a-z0-9-]+(?:,\s*[a-z0-9-]+)*)", path.read_text()):
            suppressed.update(rule.strip() for rule in match.group(1).split(","))

    assert suppressed, "no suppressions found — this test is looking in the wrong place"
    for rule in sorted(suppressed):
        assert rule in record, f"{rule} is suppressed in the tree and not explained in the record"


def test_the_gate_record_does_not_claim_a_ci_run():
    record = (REPO / "docs" / "SECURITY_GATES.md").read_text()
    assert "NOT TESTED in CI" in record
    assert "never run" in record  # cosign


def test_every_github_action_is_pinned_to_a_commit():
    """A mutable tag means two runs of one build are free to run different code."""
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        for match in re.finditer(r"uses:\s*([^\s]+)", workflow.read_text()):
            reference = match.group(1)
            if "@" not in reference:
                continue
            ref = reference.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{workflow.name} uses {reference}, which is not a commit SHA"
            )
