"""Storage: content-addressed originals, in whichever backend is configured."""

import pytest

from sutr.documentation.storage import (
    DATABASE,
    FILESYSTEM,
    OBJECT_STORE_BLOCKED,
    DocumentStorage,
    StorageError,
    digest,
)


def test_the_database_backend_hands_the_bytes_back_to_be_stored_inline():
    """The caller persists them in its own transaction, so a stored blob and a
    stored row cannot disagree."""
    store = DocumentStorage(DATABASE)
    sha, inline = store.put(b"hello")
    assert sha == digest(b"hello")
    assert inline == b"hello"
    assert store.get(sha, inline) == b"hello"


def test_the_filesystem_backend_writes_content_addressed_files(tmp_path):
    store = DocumentStorage(FILESYSTEM, str(tmp_path))
    sha, inline = store.put(b"policy text")
    assert inline is None  # nothing goes in the database
    assert store.get(sha) == b"policy text"
    # Fanned out by prefix rather than one flat directory.
    assert (tmp_path / sha[:2] / sha[2:4] / sha).exists()


def test_storing_the_same_bytes_twice_is_one_file(tmp_path):
    store = DocumentStorage(FILESYSTEM, str(tmp_path))
    first, _ = store.put(b"same")
    second, _ = store.put(b"same")
    assert first == second
    assert len(list(tmp_path.rglob(first))) == 1


def test_no_partial_file_is_left_behind(tmp_path):
    store = DocumentStorage(FILESYSTEM, str(tmp_path))
    store.put(b"content")
    assert list(tmp_path.rglob("*.part")) == []


def test_deleting_a_blob_that_is_already_gone_is_not_an_error(tmp_path):
    store = DocumentStorage(FILESYSTEM, str(tmp_path))
    sha, _ = store.put(b"gone")
    store.delete(sha)
    store.delete(sha)  # the caller asked for absence; absence is the result
    with pytest.raises(StorageError):
        store.get(sha)


def test_a_missing_inline_blob_is_an_error_not_an_empty_document():
    """Returning b"" would produce a document that parses to nothing and looks
    like a document with no content, which is a different fact entirely."""
    store = DocumentStorage(DATABASE)
    with pytest.raises(StorageError):
        store.get(digest(b"x"), None)


def test_an_object_store_backend_is_refused_rather_than_faked():
    """The LLD names object storage; no backend has been written against an
    API this repository has not exercised (build prompt §4)."""
    with pytest.raises(StorageError) as excinfo:
        DocumentStorage("s3")
    assert "DOCUMENTATION_REQUIRED" in str(excinfo.value)
    assert "DOCUMENTATION_REQUIRED" in OBJECT_STORE_BLOCKED


def test_the_filesystem_backend_requires_a_path():
    with pytest.raises(StorageError, match="DOCUMENT_STORAGE_PATH"):
        DocumentStorage(FILESYSTEM)
