"""Artifact signatures: real when a key is configured, absent and said so when not."""

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sutr.config import settings
from sutr.generation import signing

BUILD_HASH = "c" * 64


def _seed() -> str:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode()


def _pem() -> str:
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_without_a_key_nothing_is_signed_and_the_reason_says_why():
    """A placeholder signature would be worse than none."""
    assert signing.sign(BUILD_HASH) is None
    described = signing.describe()
    assert described["available"] is False
    assert "NOT_CONFIGURED" in described["unavailable_reason"]
    assert described["key_id"] is None


@pytest.mark.parametrize("material", ["seed", "pem"])
def test_a_configured_key_produces_a_verifiable_signature(monkeypatch, material):
    monkeypatch.setattr(
        settings, "generation_signing_key", _seed() if material == "seed" else _pem()
    )
    signature = signing.sign(BUILD_HASH)
    assert signature is not None
    assert signature.algorithm == "ed25519"
    assert signing.verify(BUILD_HASH, signature.value, signature.public_key)


def test_a_signature_does_not_verify_against_a_different_build_hash(monkeypatch):
    monkeypatch.setattr(settings, "generation_signing_key", _seed())
    signature = signing.sign(BUILD_HASH)
    assert signature is not None
    assert not signing.verify("d" * 64, signature.value, signature.public_key)


def test_a_signature_does_not_verify_against_another_key(monkeypatch):
    monkeypatch.setattr(settings, "generation_signing_key", _seed())
    signature = signing.sign(BUILD_HASH)
    assert signature is not None
    other = Ed25519PrivateKey.generate().public_key()
    raw = other.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    assert not signing.verify(BUILD_HASH, signature.value, base64.b64encode(raw).decode())


def test_malformed_signature_material_is_a_failed_verification_not_an_exception():
    assert signing.verify(BUILD_HASH, "not-base64!", "also-not") is False


@pytest.mark.parametrize(
    "material",
    ["not base64 at all !!", base64.b64encode(b"short").decode(), "-----BEGIN PRIVATE KEY-----\nx"],
)
def test_an_unreadable_key_is_reported_rather_than_silently_disabling_signing(
    monkeypatch, material
):
    monkeypatch.setattr(settings, "generation_signing_key", material)
    ok, reason = signing.available()
    assert ok is False
    assert reason
    assert "NOT_CONFIGURED" not in reason  # a broken key is not an absent one


def test_the_key_id_can_be_overridden_by_the_operator(monkeypatch):
    monkeypatch.setattr(settings, "generation_signing_key", _seed())
    monkeypatch.setattr(settings, "generation_signing_key_id", "release-2026")
    assert signing.sign(BUILD_HASH).key_id == "release-2026"
    assert signing.describe()["key_id"] == "release-2026"


def test_describe_states_that_it_signs_a_digest_not_an_image(monkeypatch):
    monkeypatch.setattr(settings, "generation_signing_key", _seed())
    assert signing.describe()["signs"] == "the artifact build hash, not a container image"
