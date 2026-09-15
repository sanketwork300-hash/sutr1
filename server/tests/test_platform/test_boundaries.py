"""The service boundaries are enforced, not merely declared (ADR-001, ADR-002).

A map of who-owns-what that nothing checks is a diagram. These tests turn it
into a constraint: adding an unowned module, giving two services the same
table, or importing the control plane from the hot path all fail here.
"""

import ast
import pathlib

import pytest

from sutr.platform.boundaries import (
    ALL_SERVICES,
    BOUNDARY,
    KNOWN_PLANE_VIOLATIONS,
    Plane,
    is_composition_root,
    is_known_violation,
    plane_for_module,
    service_for_module,
    service_for_table,
    services_in,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "sutr"


def _module_names() -> list[str]:
    """Every module under `sutr/`, as a dotted path relative to `sutr.`."""
    names = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(SRC)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][: -len(".py")]
        if not parts:
            continue
        names.append(".".join(parts))
    return names


def _imports_of(module: str) -> set[str]:
    """The `sutr.` modules a module imports, as dotted paths minus the prefix."""
    path = SRC / pathlib.Path(*module.split("."))
    file = path.with_suffix(".py")
    if not file.exists():
        file = path / "__init__.py"
    if not file.exists():
        return set()
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sutr."):
            found.add(node.module[len("sutr.") :])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sutr."):
                    found.add(alias.name[len("sutr.") :])
    return found


# ── Rule 1: every module belongs to exactly one service ──────────────────────


def test_every_module_belongs_to_a_service():
    orphans = [
        module
        for module in _module_names()
        if service_for_module(module) is None and not is_composition_root(module)
    ]
    assert orphans == [], (
        f"these modules belong to no service — add them to sutr/platform/boundaries.py: {orphans}"
    )


def test_no_module_is_claimed_twice():
    # `_build()` raises on a duplicate, so reaching here means it held; this
    # asserts the lookup actually got populated rather than silently empty.
    assert len(BOUNDARY.by_module) == sum(len(s.modules) for s in ALL_SERVICES)


def test_the_longest_matching_prefix_wins():
    """`integrations.types` is shared; `integrations.registry` is the registry."""
    assert service_for_module("integrations.types").name == "contracts"
    assert service_for_module("integrations.registry").name == "registry"
    assert service_for_module("integrations.bundled.stripe").name == "registry"


# ── Rule 2: every table is written by exactly one service ────────────────────


def test_every_table_has_exactly_one_owner():
    from sqlmodel import SQLModel

    import sutr.models  # noqa: F401
    from tests import conftest  # noqa: F401  — imports every model

    declared = set(BOUNDARY.by_table)
    actual = set(SQLModel.metadata.tables)
    unowned = sorted(actual - declared)
    assert unowned == [], (
        f"these tables have no owning service — add them to sutr/platform/boundaries.py: {unowned}"
    )


def test_the_map_does_not_name_tables_that_do_not_exist():
    from sqlmodel import SQLModel

    from tests import conftest  # noqa: F401

    phantom = sorted(set(BOUNDARY.by_table) - set(SQLModel.metadata.tables))
    assert phantom == [], f"the boundary map names tables that do not exist: {phantom}"


def test_table_ownership_is_looked_up_by_name():
    # The metering *write* is hot-path; billing aggregates it afterwards.
    assert service_for_table("usage_event").name == "metering"
    assert service_for_table("deployment_revision").name == "runtime_manager"
    assert service_for_table("outbox_event").name == "events"
    assert service_for_table("not_a_table") is None


# ── Rule 3: the data plane does not depend on the control plane ──────────────


def _plane_violations() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for module in _module_names():
        if plane_for_module(module) is not Plane.DATA:
            continue
        for imported in sorted(_imports_of(module)):
            if plane_for_module(imported) is Plane.CONTROL:
                found.append((module, imported))
    return found


def test_the_data_plane_takes_on_no_new_control_plane_dependencies():
    """ADR-002, and the LLD's §2.2 guarantee: when the control plane is down,
    *existing tools keep working*. A hot-path module that imports a
    control-plane module has taken a dependency on something allowed to be
    offline, and that guarantee quietly stops being true.

    Sutr does not have that separation yet, so this is a **ratchet**, not a
    clean assertion: the known violations are listed in
    `boundaries.KNOWN_PLANE_VIOLATIONS` with the phase that removes each one.
    A new one fails here. Classifying an offender as "shared" until the test
    goes green would turn an honest gap into a false claim, which is exactly
    what build prompt §83 forbids.
    """
    new_violations = [
        f"{module} imports {imported} (control plane: {service_for_module(imported).name})"
        for module, imported in _plane_violations()
        if not is_known_violation(module, imported)
    ]
    assert new_violations == [], (
        "new data-plane → control-plane dependencies (ADR-002). Either remove the import, or — "
        "if it is unavoidable for now — add it to KNOWN_PLANE_VIOLATIONS with the phase that "
        "resolves it:\n  " + "\n  ".join(new_violations)
    )


def test_the_known_violation_list_does_not_go_stale():
    """An entry that no longer describes a real import is a note about work
    already done, and it should be deleted so the list stays a true debt
    register rather than folklore."""
    actual = set(_plane_violations())
    stale = sorted(pair for pair in KNOWN_PLANE_VIOLATIONS if pair not in actual)
    assert stale == [], (
        "these entries in KNOWN_PLANE_VIOLATIONS no longer describe a real import — "
        f"delete them: {stale}"
    )


def test_every_known_violation_names_the_phase_that_removes_it():
    for pair, reason in KNOWN_PLANE_VIOLATIONS.items():
        assert "Phase" in reason, f"{pair} does not say which phase resolves it"


def test_the_debt_is_small_enough_to_be_a_debt():
    """A ratchet only works while the list is short enough that adding to it
    feels like a decision. If this fails, the separation needs real work rather
    than another entry."""
    assert len(KNOWN_PLANE_VIOLATIONS) <= 8, (
        f"{len(KNOWN_PLANE_VIOLATIONS)} known plane violations is too many to call debt"
    )


def test_shared_modules_do_not_import_either_plane():
    """Shared infrastructure is imported by both planes, so it must depend on
    neither — otherwise it drags one plane's dependencies into the other."""
    violations: list[str] = []
    for module in _module_names():
        if plane_for_module(module) is not Plane.SHARED:
            continue
        for imported in _imports_of(module):
            imported_plane = plane_for_module(imported)
            if imported_plane in (Plane.CONTROL, Plane.DATA):
                owner = service_for_module(imported)
                violations.append(
                    f"{module} imports {imported} ({imported_plane.value}: {owner.name})"
                )
    assert violations == [], "shared modules must not import a plane (ADR-001):\n  " + "\n  ".join(
        violations
    )


# ── The map itself is coherent ───────────────────────────────────────────────


def test_the_control_plane_covers_the_lld_service_list():
    """The LLD §2.3 names 15 control-plane services. Several are not built yet
    (documentation intelligence, metadata, discovery, provisioning, analytics,
    notifications, provider). This asserts what *is* mapped, so the list cannot
    silently shrink."""
    control = {service.name for service in services_in(Plane.CONTROL)}
    assert {
        "identity",
        "source_connectors",
        "translation",
        "mcp_generator",
        "runtime_manager",
        "registry",
        "marketplace",
        "governance",
        "billing",
    } <= control


@pytest.mark.parametrize("service", ALL_SERVICES, ids=lambda s: s.name)
def test_every_service_is_described(service):
    assert service.description, f"{service.name} has no description"
    assert service.modules or service.tables, f"{service.name} owns nothing"
