"""Canonical serialization of tool arguments for approval binding.

The SHA-256 of the normalized form is what a human approval is bound to, so
two argument dicts that mean the same call must hash identically:
- keys are sorted, separators are minimal (as before)
- strings are Unicode-normalized to NFC (composed and decomposed "é" match)
- integral floats collapse to ints (1.0 and 1 match — JSON producers differ)

Changing these rules invalidates hash matches against rows written under the
old rules; a pending approval from before an upgrade may need re-requesting.
"""

import hashlib
import json
import math
import unicodedata


def _canonicalize(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool):
        # bool is an int subclass — keep it before the float/int branches.
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {
            (unicodedata.normalize("NFC", k) if isinstance(k, str) else k): _canonicalize(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value


def normalize_tool_args(args: dict) -> str:
    return json.dumps(_canonicalize(args), sort_keys=True, separators=(",", ":"))


def hash_normalized_args(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
