"""The credential-key pattern is linear and still matches what it did."""

import time

import pytest

from sutr.services.redaction import REDACTED, redact_args


@pytest.mark.parametrize(
    "key",
    ["auth", "X-Auth-Token", "client_secret_v2", "api-key", "APIKEY", "db_password", "Cookie"],
)
def test_credential_keys_are_redacted(key):
    assert REDACTED in redact_args({key: "value"})


@pytest.mark.parametrize("key", ["author", "tokenizer", "passenger", "keyboard", "id"])
def test_lookalike_keys_are_not(key):
    assert REDACTED not in redact_args({key: "value"})


@pytest.mark.parametrize("key", ["-auth-" * 20_000 + "\n!", "-authx" * 20_000 + "\n!"])
def test_a_pathological_key_is_matched_in_linear_time(key):
    # The old `^(.*[_-])?(word)([_-].*)?$` form took quadratic time on many
    # `-auth-` repetitions followed by a line break (about a second at 4,000
    # repetitions, and growing with the square).
    start = time.perf_counter()
    redact_args({key: "value"})
    assert time.perf_counter() - start < 0.5
