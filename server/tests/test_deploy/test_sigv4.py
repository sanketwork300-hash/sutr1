"""AWS Signature Version 4.

The two `service` cases are AWS's own published test vectors (`get-vanilla`
and `get-vanilla-query-order-key-case` from the aws-sig-v4-test-suite), so a
regression here is checked against AWS's answer rather than against ours. The
remaining cases were verified byte-for-byte against botocore's SigV4Auth /
S3SigV4Auth at the same frozen timestamp; botocore is not a test dependency,
so the expected strings are pinned here instead.
"""

from datetime import datetime, timezone

import pytest

from sutr.deploy.aws.sigv4 import sign_request

FROZEN = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
ACCESS_KEY = "AKIDEXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def _sign(**overrides):
    kwargs = {
        "method": "GET",
        "url": "https://example.amazonaws.com/",
        "region": "us-east-1",
        "service": "service",
        "access_key_id": ACCESS_KEY,
        "secret_access_key": SECRET_KEY,
        "now": FROZEN,
    }
    kwargs.update(overrides)
    return sign_request(**kwargs)


def test_matches_the_published_get_vanilla_vector():
    header = _sign()["Authorization"]
    assert "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request" in header
    assert "SignedHeaders=host;x-amz-date" in header
    assert header.endswith(
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_query_parameters_are_sorted_before_signing():
    """AWS canonicalizes the query string; the order sent must not matter."""
    expected = "Signature=b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"
    for url in (
        "https://example.amazonaws.com/?Param1=value1&Param2=value2",
        "https://example.amazonaws.com/?Param2=value2&Param1=value1",
    ):
        assert _sign(url=url)["Authorization"].endswith(expected)


def test_json_service_signs_target_and_session_token():
    headers = _sign(
        method="POST",
        url="https://codebuild.us-east-1.amazonaws.com/",
        service="codebuild",
        session_token="SESSIONTOKEN",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "CodeBuild_20161006.StartBuild",
        },
        payload=b"{}",
    )
    assert headers["x-amz-security-token"] == "SESSIONTOKEN"
    assert (
        "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target"
        in headers["Authorization"]
    )
    assert headers["Authorization"].endswith(
        "Signature=7203ed8df0b1105b3e9b1707677a92449ee4fd42d873d85092568ba0f98c5f93"
    )
    # Only S3 requires the payload-hash header; adding it elsewhere would
    # enlarge SignedHeaders and break every published vector.
    assert "x-amz-content-sha256" not in headers


def test_s3_signs_the_payload_hash_header():
    headers = _sign(
        method="PUT",
        url="https://s3.us-west-2.amazonaws.com/bucket/packages/ab.zip",
        region="us-west-2",
        service="s3",
        session_token="TOK",
        headers={"Content-Type": "application/zip"},
        payload=b"payload",
    )
    assert headers["x-amz-content-sha256"] == (
        "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"
    )
    assert headers["Authorization"].endswith(
        "Signature=f5f4ba048c1e8118223e1edcafbe34392f1eb3921389aedcd7e20b616c405069"
    )


def test_host_is_not_returned_as_a_header():
    """It is signed, but httpx sets it from the URL; sending it duplicates it."""
    assert "host" not in _sign()


def test_signature_changes_with_the_payload():
    empty = _sign(method="POST", payload=b"")["Authorization"]
    filled = _sign(method="POST", payload=b"{}")["Authorization"]
    assert empty != filled


@pytest.mark.parametrize("service", ["s3", "codebuild"])
def test_signing_is_deterministic_for_a_fixed_clock(service):
    assert _sign(service=service) == _sign(service=service)
