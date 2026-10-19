"""Encrypted-at-rest secrets in the database, with a key from configuration.

The default backend. Before it existed the default (`db`) wrote every stored
credential — GitHub tokens, cloud refresh tokens, custom API keys — into
`Secret.value` as plaintext, so any copy of the database or a backup was a
copy of every tenant's upstream access.

What this does: AES-256-GCM per secret, a fresh 12-byte nonce each time, and
the row's `ref` bound in as associated data so a ciphertext cannot be moved
onto another row by someone who can write to the database. The key comes from
the environment, never from the data volume:

    SECRETS_ENCRYPTION_KEY   32 bytes, base64 or hex — set this in production
    (unset)                  HKDF-SHA256 of JWT_SECRET_KEY with a distinct
                             info string, so a secrets key and a signing key
                             are separated even when one value is configured

What this is NOT: envelope encryption with a managed key service (that is
`db_kms`) or an external store (`vault`). A database dump plus the environment
still decrypts everything; the point is that the dump alone does not.

Stored form, in `Secret.value`:

    "enc1:" + base64(nonce || ciphertext-with-tag)

A row whose value does not carry the prefix is one written by the old `db`
backend and is returned as-is, so an upgrade does not lock anyone out of
their credentials. `reencrypt_legacy_secrets` in `secrets/records.py` then
converts those rows in place.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets.base import SecretsBackend, StoredSecret

logger = logging.getLogger(__name__)

BACKEND_NAME = "db_encrypted"
# Versioned so a future format (a different cipher, a key id) can coexist with
# rows written under this one.
PREFIX = "enc1:"
NONCE_BYTES = 12
KEY_BYTES = 32
# Distinct from provisioning/passes.py's info string on purpose: a key derived
# for signing access passes must never be the key that decrypts credentials.
_KEY_INFO = b"sutr-secrets-at-rest-v1"


class SecretDecryptionError(RuntimeError):
    """The stored ciphertext does not verify under the configured key.

    Raised, not swallowed: a credential that silently reads as None looks like
    "not configured" to every caller, and the real cause — a rotated or
    mistyped SECRETS_ENCRYPTION_KEY — would go unreported.
    """


def _hkdf(secret: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 with an empty salt (RFC 5869)."""
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def decode_key(configured: str) -> bytes:
    """Accept the two encodings an operator is likely to produce.

    `openssl rand -hex 32` gives 64 hex characters; `openssl rand -base64 32`
    gives 44 characters of base64. Anything that does not decode to exactly 32
    bytes is refused at startup rather than silently padded or truncated.
    """
    text = configured.strip()
    if len(text) == KEY_BYTES * 2:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    padded = text + "=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(raw) == KEY_BYTES:
            return raw
    raise RuntimeError(
        "SECRETS_ENCRYPTION_KEY must be 32 bytes encoded as hex (64 chars) or base64 "
        "(44 chars). Generate one with: openssl rand -hex 32"
    )


def derive_key() -> bytes:
    """The AES key this process encrypts and decrypts with.

    An explicit SECRETS_ENCRYPTION_KEY when the operator set one. Otherwise
    derived from JWT_SECRET_KEY by HKDF with a distinct info string: the two
    keys come from one configured secret but are not interchangeable, and a
    leak of one is not a leak of the other.
    """
    explicit = settings.secrets_encryption_key.strip()
    if explicit:
        return decode_key(explicit)
    return _hkdf(settings.jwt_secret_key.encode("utf-8"), _KEY_INFO, KEY_BYTES)


def _aad(ref: str | None) -> bytes:
    return (ref or "").encode("utf-8")


class LocalEncryptedSecretsBackend(SecretsBackend):
    """AES-256-GCM in the Secret.value column, keyed from the environment."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key if key is not None else derive_key()
        if len(self._key) != KEY_BYTES:
            raise RuntimeError("secrets encryption key must be exactly 32 bytes")
        self._aesgcm = AESGCM(self._key)

    def store(self, ref: str, value: str) -> StoredSecret:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, value.encode("utf-8"), _aad(ref))
        payload = PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")
        return StoredSecret(value=payload, encrypted_data_key=None, kms_key_id=None)

    def retrieve(self, secret: Secret) -> str | None:
        if secret.value is None:
            return None
        if not secret.value.startswith(PREFIX):
            # Written by the plaintext `db` backend before this one existed.
            # Readable so an upgrade does not break every stored credential;
            # the startup sweep rewrites it encrypted.
            return secret.value
        try:
            payload = base64.b64decode(secret.value[len(PREFIX) :])
        except (binascii.Error, ValueError) as exc:
            raise SecretDecryptionError(f"secret {secret.id}: malformed ciphertext") from exc
        if len(payload) <= NONCE_BYTES:
            raise SecretDecryptionError(f"secret {secret.id}: malformed ciphertext")
        nonce, ciphertext = payload[:NONCE_BYTES], payload[NONCE_BYTES:]
        try:
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, _aad(secret.ref))
        except InvalidTag as exc:
            raise SecretDecryptionError(
                f"secret {secret.id}: ciphertext does not verify. The key it was written "
                "under (SECRETS_ENCRYPTION_KEY, or JWT_SECRET_KEY when that is unset) is not "
                "the one configured now."
            ) from exc
        return plaintext.decode("utf-8")

    def delete(self, secret: Secret) -> None:
        pass


def is_encrypted(value: str | None) -> bool:
    """Whether a stored value was written by this backend."""
    return bool(value) and value.startswith(PREFIX)
