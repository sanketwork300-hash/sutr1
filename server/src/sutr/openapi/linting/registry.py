"""Rule registration.

A rule is a pure function over the parsed document that yields findings. It
never raises: a rule that cannot evaluate a malformed fragment skips it, so a
single odd corner of a specification cannot suppress every other finding — the
whole point of build prompt §16 is to return *all* findings, not the first.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sutr.openapi.linting.finding import LintFinding, Severity

logger = logging.getLogger(__name__)

RuleFn = Callable[[dict], Iterable[LintFinding]]


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    summary: str
    check: RuleFn
    # Which spec versions the rule applies to: "3.0", "3.1", or both.
    versions: tuple[str, ...] = ("3.0", "3.1")


_RULES: dict[str, Rule] = {}


def rule(
    rule_id: str,
    severity: Severity,
    summary: str,
    versions: tuple[str, ...] = ("3.0", "3.1"),
) -> Callable[[RuleFn], RuleFn]:
    def register(fn: RuleFn) -> RuleFn:
        if rule_id in _RULES:
            raise ValueError(f"duplicate lint rule id {rule_id!r}")
        _RULES[rule_id] = Rule(
            id=rule_id, severity=severity, summary=summary, check=fn, versions=versions
        )
        return fn

    return register


def all_rules() -> list[Rule]:
    return sorted(_RULES.values(), key=lambda r: r.id)


def run_rules(document: dict, version: str) -> list[LintFinding]:
    """Run every applicable rule and collect the findings.

    A rule that raises is logged and skipped rather than failing the import:
    linting is advisory, and a bug in one rule must not stop a user importing
    a valid specification.
    """
    findings: list[LintFinding] = []
    short = ".".join(version.split(".")[:2]) if version else "3.0"
    for entry in all_rules():
        if short not in entry.versions:
            continue
        try:
            findings.extend(entry.check(document))
        except Exception:  # pragma: no cover — defensive
            logger.warning("lint rule %s failed; skipping", entry.id, exc_info=True)
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.rule_id, f.json_pointer))
    return findings


_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    @property
    def infos(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == Severity.INFO]

    def counts(self) -> dict[str, int]:
        return {
            "error": len(self.errors),
            "warning": len(self.warnings),
            "info": len(self.infos),
        }
