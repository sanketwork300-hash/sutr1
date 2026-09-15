"""Shared traversal helpers for lint rules.

Rules receive the raw parsed document (not the IR), because a lint finding has
to point at the user's own file — the IR has already dropped and rewritten
things, and a pointer into it would be meaningless to the author.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from sutr.openapi.linting.finding import pointer

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


@dataclass(frozen=True)
class OperationRef:
    path: str
    method: str  # lower-case, as written in the document
    operation: dict
    path_item: dict

    @property
    def location(self) -> str:
        return f"{self.method.upper()} {self.path}"

    @property
    def json_pointer(self) -> str:
        return pointer("paths", self.path, self.method)


def paths(document: dict) -> dict:
    value = document.get("paths")
    return value if isinstance(value, dict) else {}


def iter_operations(document: dict) -> Iterator[OperationRef]:
    for path, path_item in paths(document).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                yield OperationRef(
                    path=str(path), method=method, operation=operation, path_item=path_item
                )


def effective_parameters(ref: OperationRef) -> list[tuple[dict, str]]:
    """Path-item parameters plus operation parameters, with their pointers."""
    result: list[tuple[dict, str]] = []
    for index, param in enumerate(ref.path_item.get("parameters") or []):
        if isinstance(param, dict):
            result.append((param, pointer("paths", ref.path, "parameters", index)))
    for index, param in enumerate(ref.operation.get("parameters") or []):
        if isinstance(param, dict):
            result.append((param, pointer("paths", ref.path, ref.method, "parameters", index)))
    return result
