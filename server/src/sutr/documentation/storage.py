"""Where the original document bytes live.

Every extracted fact cites the sentence it came from, which means the original
has to still be there when someone asks "says who?" — so this is not a cache,
it is the evidence.

Two backends, and the choice is deliberate:

- **database** (default): the bytes sit in `document.content`. Same decision
  already taken for deployment packages (ADR-015), same trade-off — one
  durable store, no second thing to back up, and a hard ceiling on size.
- **filesystem**: content-addressed files under a configured directory, for
  deployments where documents are large enough that the database is the wrong
  home for them.

There is no S3/MinIO backend. The LLD names object storage, but writing one
against an interface nobody here has exercised would be guesswork of exactly
the kind the build prompt forbids; the seam is here so it is a backend and not
a rewrite when MinIO is actually deployed.
"""

import hashlib
import os
from pathlib import Path

from sutr.config import settings

DATABASE = "database"
FILESYSTEM = "filesystem"

OBJECT_STORE_BLOCKED = (
    "NOT_CONFIGURED / DOCUMENTATION_REQUIRED: no object-storage backend is "
    "implemented. Set DOCUMENT_STORAGE_BACKEND to 'database' or 'filesystem'."
)


class StorageError(Exception):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DocumentStorage:
    """Content-addressed blob storage for uploaded documents."""

    def __init__(self, backend: str, root: str | None = None) -> None:
        if backend not in (DATABASE, FILESYSTEM):
            raise StorageError(OBJECT_STORE_BLOCKED)
        self.backend = backend
        self.root = Path(root).expanduser() if root else None
        if self.backend == FILESYSTEM and self.root is None:
            raise StorageError(
                "DOCUMENT_STORAGE_BACKEND=filesystem requires DOCUMENT_STORAGE_PATH."
            )

    # The database backend deliberately returns the bytes back to the caller:
    # the caller writes them onto the Document row inside its own transaction,
    # so a stored blob and a stored row can never disagree.
    def put(self, data: bytes) -> tuple[str, bytes | None]:
        """Store `data`; return its digest and the bytes to persist inline."""
        sha = digest(data)
        if self.backend == DATABASE:
            return sha, data
        path = self._path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Write-then-rename: a reader never sees a half-written document.
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return sha, None

    def get(self, sha: str, inline: bytes | None = None) -> bytes:
        if self.backend == DATABASE:
            if inline is None:
                raise StorageError(f"Document {sha} has no stored content.")
            return inline
        path = self._path(sha)
        if not path.exists():
            raise StorageError(f"Document {sha} is missing from {self.root}.")
        return path.read_bytes()

    def delete(self, sha: str) -> None:
        """Remove a blob if this backend owns one.

        Deliberately best-effort: a blob that is already gone is the state the
        caller asked for.
        """
        if self.backend == FILESYSTEM:
            self._path(sha).unlink(missing_ok=True)

    def _path(self, sha: str) -> Path:
        if len(sha) < 4:
            raise StorageError(f"Not a content digest: {sha!r}")
        # Fan out by prefix: one flat directory of a million files is a
        # different kind of problem.
        return self.root / sha[:2] / sha[2:4] / sha


_storage: DocumentStorage | None = None


def get_storage() -> DocumentStorage:
    global _storage
    if _storage is None:
        _storage = DocumentStorage(
            settings.document_storage_backend,
            settings.document_storage_path or None,
        )
    return _storage


def set_storage(storage: DocumentStorage | None) -> None:
    """Override the process-wide storage. Used by tests and by startup."""
    global _storage
    _storage = storage


def describe() -> dict:
    storage = get_storage()
    return {
        "backend": storage.backend,
        "root": str(storage.root) if storage.root else None,
        "object_store": {"available": False, "unavailable_reason": OBJECT_STORE_BLOCKED},
    }
