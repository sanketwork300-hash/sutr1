"""A CycloneDX software bill of materials for a generated runtime artifact.

LLD §3.6 requires an *"SBOM per build"*. This produces one, in CycloneDX 1.5
JSON, covering both halves of what a generated package actually contains:

- **the files it ships**, each with its SHA-256, because the generated code is
  the majority of what runs and an inventory that skipped it would describe the
  smaller half of the artifact;
- **the dependencies it declares**, read from the package's own
  `requirements.txt`.

One limitation is stated in the document itself rather than left for a reader
to discover. The dependency entries carry **declared constraints, not resolved
versions**: the package pins ranges and there is no lock file, so the exact
version that ends up installed is decided by whoever builds the image, from an
index this generator never contacts. A `version` field filled with a guess
would be worse than an empty one, so the constraint goes in a property and the
version stays empty. Resolving it properly needs a build that actually installs
the dependencies, which is the isolated build worker the LLD asks for and this
install does not have (ADR-038).

The document is deterministic: no timestamps, and the serial number is derived
from the artifact's own content, so rebuilding an unchanged artifact produces a
byte-identical SBOM.
"""

import hashlib
import json
import re
import uuid
from typing import Any

SPEC_VERSION = "1.5"
BOM_FORMAT = "CycloneDX"

# A fixed namespace so `serialNumber` is a pure function of the artifact.
_SERIAL_NAMESPACE = uuid.UUID("6f0f0f4c-6a54-5f6b-9a4b-2a1b3c4d5e6f")

_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$")


def parse_requirements(text: str) -> list[dict[str, str]]:
    """Requirement lines → {name, extras, constraint}, comments dropped."""
    entries: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT.match(line)
        if not match:
            continue
        name, extras, constraint = match.groups()
        entries.append(
            {
                "name": name,
                "extras": (extras or "").strip("[]"),
                "constraint": (constraint or "").strip(),
            }
        )
    return entries


def _file_component(path: str, content: str) -> dict[str, Any]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "type": "file",
        "name": path,
        "bom-ref": f"file:{path}",
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [{"name": "sutr:bytes", "value": str(len(content.encode("utf-8")))}],
    }


def _library_component(entry: dict[str, str]) -> dict[str, Any]:
    properties = [{"name": "sutr:declared-constraint", "value": entry["constraint"] or "*"}]
    if entry["extras"]:
        properties.append({"name": "sutr:extras", "value": entry["extras"]})
    properties.append(
        {
            "name": "sutr:version-resolution",
            "value": "unresolved - the package declares a range and is installed at build time",
        }
    )
    return {
        "type": "library",
        "name": entry["name"],
        "version": "",
        "bom-ref": f"pkg:pypi/{entry['name']}",
        "purl": f"pkg:pypi/{entry['name']}",
        "scope": "required",
        "properties": properties,
    }


def build_sbom(
    *,
    files: dict[str, str],
    manifest: dict[str, Any],
    package_sha256: str,
    generator_version: str,
) -> dict[str, Any]:
    """The CycloneDX document for one artifact."""
    requirements = parse_requirements(files.get("requirements.txt", ""))
    components = [_library_component(entry) for entry in requirements]
    components += [_file_component(path, files[path]) for path in sorted(files)]

    root = {
        "type": "application",
        "name": manifest.get("slug") or manifest.get("name", ""),
        "version": (manifest.get("api") or {}).get("version", "") or "0",
        "bom-ref": f"artifact:{package_sha256}",
        "hashes": [{"alg": "SHA-256", "content": package_sha256}],
        "properties": [
            {"name": "sutr:runtime", "value": manifest.get("runtime", "")},
            {"name": "sutr:template-version", "value": manifest.get("template_version", "")},
            {"name": "sutr:ir-hash", "value": (manifest.get("source") or {}).get("ir_hash", "")},
            {
                "name": "sutr:knowledge-hash",
                "value": (manifest.get("source") or {}).get("knowledge_hash", ""),
            },
        ],
    }

    document = {
        "bomFormat": BOM_FORMAT,
        "specVersion": SPEC_VERSION,
        "version": 1,
        "metadata": {
            "tools": [
                {
                    "vendor": "sutr",
                    "name": "sutr-mcp-generator",
                    "version": generator_version,
                }
            ],
            "component": root,
        },
        "components": components,
    }
    serial = uuid.uuid5(
        _SERIAL_NAMESPACE,
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )
    document["serialNumber"] = f"urn:uuid:{serial}"
    return document


def component_counts(document: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for component in document.get("components", []):
        kind = component.get("type", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts
