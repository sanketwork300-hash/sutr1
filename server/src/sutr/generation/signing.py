"""Detached signatures over a runtime artifact's build hash.

LLD §3.6 asks for *signed images*, via Cosign in the technology stack (p. 30).
This install does not build images and has no registry to push them to, so
there is nothing for Cosign to sign — and adding a Cosign integration against a
registry this code has never talked to would be inventing the part that matters
(build prompt §4).

What is real here, and is therefore what ships: an **Ed25519 signature over the
artifact's build hash**, made with a key the operator holds. That is a genuine
cryptographic assertion — *this platform, holding this key, produced an
artifact with this digest* — and it is verifiable by anyone with the public
key, offline. It is not Sigstore: there is no transparency log, no certificate
chain, and no keyless identity. ADR-038 records that distinction so nobody
reads "signed" here and assumes the other thing.

With no key configured, artifacts are unsigned and say so. A placeholder
signature would be worse than none, because a reader who saw a `signature`
field would reasonably believe it meant something.
"""

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sutr.config import settings

ALGORITHM = "ed25519"

NO_KEY = (
    "NOT_CONFIGURED: no artifact signing key is set (GENERATION_SIGNING_KEY), so artifacts are "
    "stored unsigned. Set one to a PEM-encoded Ed25519 private key or a base64 32-byte seed."
)


class SigningKeyError(ValueError):
    """The configured signing key could not be read."""


@dataclass
class Signature:
    algorithm: str
    key_id: str
    public_key: str
    value: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "public_key": self.public_key,
            "value": self.value,
        }


def _load_private_key(material: str) -> Ed25519PrivateKey:
    """Read the configured key, PEM or raw seed.

    Two forms because two situations: an operator with a key management story
    already has PEM, and an operator running a container has an environment
    variable, where a single-line base64 seed is the practical shape.
    """
    text = material.strip()
    if not text:
        raise SigningKeyError(NO_KEY)
    if "BEGIN" in text:
        try:
            key = serialization.load_pem_private_key(text.encode("utf-8"), password=None)
        except (ValueError, TypeError) as exc:
            raise SigningKeyError(f"The signing key is not a readable PEM private key: {exc}")
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningKeyError(
                "The signing key must be Ed25519; a key of another type was configured."
            )
        return key
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SigningKeyError(f"The signing key is neither PEM nor valid base64: {exc}")
    if len(raw) != 32:
        raise SigningKeyError(
            f"An Ed25519 seed is 32 bytes; the configured key decodes to {len(raw)}."
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def key_id_for(public_key: Ed25519PublicKey) -> str:
    """A short, stable identifier for a key: the first 16 hex of its public bytes."""
    return _public_bytes(public_key).hex()[:16]


def available() -> tuple[bool, str | None]:
    if not settings.generation_signing_key.strip():
        return False, NO_KEY
    try:
        _load_private_key(settings.generation_signing_key)
    except SigningKeyError as exc:
        return False, str(exc)
    return True, None


def describe() -> dict[str, Any]:
    ok, reason = available()
    entry: dict[str, Any] = {
        "available": ok,
        "unavailable_reason": reason,
        "algorithm": ALGORITHM if ok else None,
        "signs": "the artifact build hash, not a container image",
        "key_id": None,
        "public_key": None,
    }
    if ok:
        key = _load_private_key(settings.generation_signing_key)
        public = key.public_key()
        entry["key_id"] = settings.generation_signing_key_id or key_id_for(public)
        entry["public_key"] = base64.b64encode(_public_bytes(public)).decode()
    return entry


def sign(build_hash: str) -> Signature | None:
    """Sign a build hash, or return None when no key is configured.

    The signature covers the hex digest as ASCII, which is what a verifier
    reads off the artifact record — signing the raw bytes instead would make
    verification depend on decoding the digest the same way.
    """
    ok, _reason = available()
    if not ok:
        return None
    key = _load_private_key(settings.generation_signing_key)
    public = key.public_key()
    return Signature(
        algorithm=ALGORITHM,
        key_id=settings.generation_signing_key_id or key_id_for(public),
        public_key=base64.b64encode(_public_bytes(public)).decode(),
        value=base64.b64encode(key.sign(build_hash.encode("ascii"))).decode(),
    )


def verify(build_hash: str, signature: str, public_key: str) -> bool:
    """Check a stored signature. Any malformed input is a failed verification."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), build_hash.encode("ascii"))
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False
    return True
