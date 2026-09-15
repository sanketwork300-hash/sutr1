"""Shared fixtures for the documentation pipeline tests."""

import pytest

from sutr.documentation import embeddings, storage

# A small policy document with one of everything the extractors look for: a
# deontic rule with a condition, a numbered procedure, and a definition.
POLICY = """# Refund Policy

## Eligibility

Refunds are allowed only within 30 days of purchase. Customers must provide the
original order number. Refunds are not permitted for digital goods that have
been downloaded.

## Refund Process

1. Submit Refund Request
2. Verify Payment
3. Approve Refund
4. Issue Credit

## Definitions

Chargeback means a reversal of a card payment initiated by the cardholder's
bank.
"""


@pytest.fixture(autouse=True)
def _reset_documentation_globals():
    """Storage and embedding providers are process-wide; tests must not leak.

    Both are deliberately module-level singletons (they are configuration, not
    per-request state), which makes resetting them the test suite's job.
    """
    storage.set_storage(None)
    embeddings.set_provider(embeddings.UnconfiguredProvider())
    yield
    storage.set_storage(None)
    embeddings.set_provider(embeddings.UnconfiguredProvider())
