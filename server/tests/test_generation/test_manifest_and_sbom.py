"""The two documents that describe an artifact without unzipping it."""

import json

from sutr.generation import manifest as manifest_module
from sutr.generation import sbom as sbom_module

from .conftest import build_files, build_manifest


def test_a_manifest_is_byte_identical_for_identical_inputs():
    """The determinism requirement, at the level the build hash depends on."""
    first = build_manifest(build_files())
    second = build_manifest(build_files())
    assert manifest_module.canonical_json(first) == manifest_module.canonical_json(second)
    assert manifest_module.manifest_hash(first) == manifest_module.manifest_hash(second)


def test_a_manifest_digests_every_file_it_lists():
    files = build_files()
    manifest = build_manifest(files)
    listed = {entry["path"]: entry for entry in manifest["files"]}
    assert set(listed) == set(files)
    for path, entry in listed.items():
        assert entry["sha256"] == manifest_module._digest(files[path])
        assert entry["bytes"] == len(files[path].encode())


def test_changing_one_generated_byte_changes_the_manifest_hash():
    files = build_files()
    before = manifest_module.manifest_hash(build_manifest(files))
    files["README.md"] += "\n"
    assert manifest_module.manifest_hash(build_manifest(files)) != before


def test_the_manifest_names_the_credential_variable_and_marks_it_secret():
    manifest = build_manifest(build_files())
    entries = {entry["name"]: entry for entry in manifest["environment"]}
    credential = entries["PETSTORE_KIT_API_TOKEN"]
    assert credential["required"] is True
    assert credential["secret"] is True
    # Governance variables are declared too, and are not required.
    assert entries["GOVERNANCE_MODE"]["required"] is False


def test_an_unauthenticated_package_declares_no_credential_variable():
    manifest = build_manifest(build_files(token_header="", token_format=""))
    names = {entry["name"] for entry in manifest["environment"]}
    assert not any(name.endswith("_API_TOKEN") for name in names)


def test_the_sbom_is_cyclonedx_and_covers_files_and_dependencies():
    files = build_files()
    manifest = build_manifest(files)
    document = sbom_module.build_sbom(
        files=files, manifest=manifest, package_sha256="d" * 64, generator_version="1"
    )
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.5"
    assert document["serialNumber"].startswith("urn:uuid:")
    counts = sbom_module.component_counts(document)
    assert counts["file"] == len(files)
    assert counts["library"] >= 4
    assert document["metadata"]["component"]["hashes"][0]["content"] == "d" * 64


def test_the_sbom_says_dependency_versions_are_unresolved_rather_than_guessing():
    files = build_files()
    document = sbom_module.build_sbom(
        files=files,
        manifest=build_manifest(files),
        package_sha256="d" * 64,
        generator_version="1",
    )
    libraries = [c for c in document["components"] if c["type"] == "library"]
    httpx = next(c for c in libraries if c["name"] == "httpx")
    # An empty version rather than a plausible-looking one: the constraint is
    # what the package declares, and the resolved version is decided later.
    assert httpx["version"] == ""
    properties = {p["name"]: p["value"] for p in httpx["properties"]}
    assert properties["sutr:declared-constraint"] == ">=0.27.0"
    assert "unresolved" in properties["sutr:version-resolution"]


def test_the_sbom_serial_number_is_derived_from_content_not_generated_fresh():
    files = build_files()
    manifest = build_manifest(files)
    first = sbom_module.build_sbom(
        files=files, manifest=manifest, package_sha256="d" * 64, generator_version="1"
    )
    second = sbom_module.build_sbom(
        files=files, manifest=manifest, package_sha256="d" * 64, generator_version="1"
    )
    assert first == second
    other = sbom_module.build_sbom(
        files=files, manifest=manifest, package_sha256="e" * 64, generator_version="1"
    )
    assert other["serialNumber"] != first["serialNumber"]


def test_requirement_parsing_drops_comments_and_keeps_extras():
    entries = sbom_module.parse_requirements(
        "# a comment\nmcp>=1.9.0,<2.0\nuvicorn[standard]>=0.30.0  # trailing\n\n"
    )
    assert entries == [
        {"name": "mcp", "extras": "", "constraint": ">=1.9.0,<2.0"},
        {"name": "uvicorn", "extras": "standard", "constraint": ">=0.30.0"},
    ]


def test_knowledge_reaches_the_generated_bundle_with_its_citations():
    knowledge = {
        "rules": [
            {
                "id": "1",
                "type": "eligibility",
                "condition": "within 30 days",
                "action": "allow refund",
                "source": {"document": "policy.md", "location": "Eligibility", "text": "..."},
            }
        ],
        "terms": [],
        "workflows": [],
        "by_tool": {},
        "truncated": {},
    }
    files = build_files(knowledge=knowledge)
    bundle = json.loads(files["tools.json"])
    assert bundle["knowledge"]["rules"][0]["source"]["document"] == "policy.md"
