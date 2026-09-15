"""IR identity: version, canonical form, and content hash.

Build prompt §14 requires the IR to be *deterministic, versioned, inspectable,
reproducible* — "same specification + same compiler version must produce the
same IR". Two things are needed for that to be checkable rather than merely
intended:

- a **version** that changes when the compiler's output shape changes, so an
  IR produced by an older build is recognisable rather than silently mixed in;
- a **canonical form** whose hash is stable, so two IRs can be compared without
  comparing every field by hand.

The hash covers the IR's *meaning*, not its serialization. Key order, and the
warnings and lint findings the compiler happened to emit, are excluded: two
runs that produce the same operations, servers and schemes describe the same
API even if one of them also mentioned a missing licence field.
"""

import hashlib
import json
from typing import Any

# Bumped whenever the IR's shape changes in a way that alters its meaning —
# a new field that carries semantics, a changed normalization rule. Not bumped
# for a new warning code or a docstring.
#
# History:
#   1  initial IR (servers, security schemes, operations)
#   2  body encodings (form/multipart/binary/text) and OAuth2 flows on schemes
IR_VERSION = 2

# Excluded from the fingerprint: advisory output and provenance, not meaning.
#
# `source_dialect` is provenance: an API migrated from Swagger 2.0 to OpenAPI
# 3.x with no semantic change is the same API, and reporting that migration as
# drift would be exactly the reformat-noise this hash exists to avoid.
_ADVISORY_FIELDS = ("warnings", "lint_findings", "source_dialect")


def canonical(definition) -> dict:
    """The IR reduced to what determines its meaning.

    Sorted, advisory fields removed, so the result is stable across runs and
    across the order a dict happened to be built in.
    """
    payload = definition.model_dump(mode="json")
    for field in _ADVISORY_FIELDS:
        payload.pop(field, None)
    return payload


def canonical_json(definition) -> str:
    return json.dumps(canonical(definition), sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(definition) -> str:
    """A stable content hash of the IR's meaning."""
    return hashlib.sha256(canonical_json(definition).encode()).hexdigest()


def content_hash(text: str) -> str:
    """A hash of the raw source document, before any parsing.

    Distinct from `fingerprint`: a source can change — reformatted, recommented,
    keys reordered — without its IR changing at all. Comparing both is how sync
    tells "the file moved" from "the API moved".
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def describe(definition) -> dict[str, Any]:
    """Identity fields for storage and for the API."""
    return {
        "ir_version": IR_VERSION,
        "ir_hash": fingerprint(definition),
        "operation_count": len(definition.operations),
    }
