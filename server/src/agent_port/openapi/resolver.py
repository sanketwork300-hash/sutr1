"""Cycle-safe resolution of internal `$ref` pointers in an OpenAPI document.

Design (Sutr spec §18):
- Only internal references (`#/...`) are resolved. External references (files,
  URLs) are rejected outright — following them from an untrusted document is
  an SSRF/file-read primitive.
- A resolution *stack* detects cycles: when a pointer already on the stack is
  referenced again, the reference is replaced by a bounded placeholder object
  instead of recursing (A → B → C → A terminates).
- A depth cap backstops pathological non-cyclic nesting.
- Resolution is applied structurally (deep-copy semantics), so the result is a
  plain dict tree that can be traversed without ever consulting `$ref` again.
  Memoization is deliberately NOT applied across different stack contexts:
  whether a pointer is a cycle depends on the path that reached it.
"""

from typing import Any

from agent_port.openapi.errors import OpenAPIError
from agent_port.openapi.limits import MAX_REF_DEPTH


def _lookup_pointer(document: dict, pointer: str) -> Any:
    """Resolve a JSON Pointer like '#/components/schemas/Pet'."""
    if not pointer.startswith("#/"):
        raise OpenAPIError(
            "external_ref",
            f"External or non-local $ref '{pointer}' is not allowed. "
            "Inline all external references before importing.",
        )
    node: Any = document
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                raise OpenAPIError("broken_ref", f"$ref '{pointer}' does not resolve.")
        else:
            raise OpenAPIError("broken_ref", f"$ref '{pointer}' does not resolve.")
    return node


def _cycle_placeholder(pointer: str) -> dict:
    name = pointer.rsplit("/", 1)[-1]
    return {
        "description": f"(recursive reference to {name})",
        "x-recursive-ref": pointer,
    }


def resolve_refs(document: dict) -> dict:
    """Return a copy of `document` with all internal $refs inlined.

    Cycles become bounded placeholder objects; external refs and broken
    pointers raise OpenAPIError.
    """

    # Ref-hop depth and structural nesting are bounded separately: a normal
    # spec nests structures far deeper than it chains references. A total node
    # budget additionally defeats diamond-shaped reference graphs whose
    # expansion is exponential ("billion laughs" via $ref).
    max_structural_depth = 400
    max_output_nodes = 500_000
    node_budget = [max_output_nodes]

    def resolve(node: Any, stack: tuple[str, ...], structural_depth: int) -> Any:
        node_budget[0] -= 1
        if node_budget[0] < 0:
            raise OpenAPIError(
                "expansion_too_large",
                "Resolving the document's references expands it beyond the allowed size.",
            )
        if len(stack) > MAX_REF_DEPTH:
            raise OpenAPIError(
                "ref_depth_exceeded",
                f"Reference nesting exceeds the maximum depth of {MAX_REF_DEPTH}.",
            )
        if structural_depth > max_structural_depth:
            raise OpenAPIError("document_too_deep", "The document is nested too deeply to process.")

        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in stack:
                    return _cycle_placeholder(ref)
                target = _lookup_pointer(document, ref)
                resolved = resolve(target, stack + (ref,), structural_depth + 1)
                # Per JSON Reference, siblings of $ref are ignored in 3.0; in
                # 3.1 `description`/`summary` may sit alongside and win.
                if isinstance(resolved, dict):
                    extras = {k: v for k, v in node.items() if k in ("description", "summary")}
                    if extras:
                        resolved = {**resolved, **extras}
                return resolved
            return {k: resolve(v, stack, structural_depth + 1) for k, v in node.items()}

        if isinstance(node, list):
            return [resolve(item, stack, structural_depth + 1) for item in node]

        return node

    return resolve(document, (), 0)
