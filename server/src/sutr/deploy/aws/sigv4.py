"""AWS Signature Version 4, in about a hundred lines of stdlib.

boto3 is an optional extra in this project (it exists only for the KMS secrets
backend), and pulling it in as a hard dependency to make a handful of REST
calls would be a poor trade. SigV4 is a well-specified algorithm; this is a
direct implementation of it.

Scope, deliberately narrow: single-chunk signing of a request whose payload we
already hold in memory. No streaming, no presigned URLs, no chunked uploads.
The generated packages are a few hundred kilobytes, so the whole body is
always available to hash.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = _sign(f"AWS4{secret_key}".encode(), date_stamp)
    key = _sign(key, region)
    key = _sign(key, service)
    return _sign(key, "aws4_request")


def _canonical_uri(path: str, service: str) -> str:
    """The path, encoded the way each service expects.

    S3 treats the object key as a literal path and must not have its slashes
    or already-encoded characters touched again; every other service wants
    each segment percent-encoded. Getting this backwards produces a signature
    mismatch that says nothing about why.
    """
    if not path:
        return "/"
    if service == "s3":
        return path
    return quote(path, safe="/~")


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((quote(name, safe="~"), quote(value, safe="~")))
    pairs.sort()
    return "&".join(f"{name}={value}" for name, value in pairs)


def sign_request(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str = "",
    headers: dict[str, str] | None = None,
    payload: bytes = b"",
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the headers to send, including Authorization.

    The caller's headers are preserved; Host, X-Amz-Date, the payload hash,
    and the session token are added because they are part of what gets signed.
    """
    parsed = urlsplit(url)
    host = parsed.netloc
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = stamp[:8]
    payload_hash = hashlib.sha256(payload).hexdigest() if payload else EMPTY_PAYLOAD_HASH

    signed: dict[str, str] = dict(headers or {})
    signed["host"] = host
    signed["x-amz-date"] = stamp
    if service == "s3":
        # S3 requires the payload hash as a header; the JSON-protocol services
        # do not, and sending it there only enlarges SignedHeaders.
        signed["x-amz-content-sha256"] = payload_hash
    if session_token:
        signed["x-amz-security-token"] = session_token

    # Header names lowercase and sorted; values trimmed. Only headers we
    # actually sign go into SignedHeaders, and we sign everything we send so
    # a proxy cannot slip one in unnoticed.
    canonical_pairs = sorted(
        (name.lower(), " ".join(str(value).split())) for name, value in signed.items()
    )
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in canonical_pairs)
    signed_headers = ";".join(name for name, _ in canonical_pairs)

    canonical_request = "\n".join(
        [
            method.upper(),
            _canonical_uri(parsed.path, service),
            _canonical_query(parsed.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            stamp,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_access_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    signed["Authorization"] = (
        f"{ALGORITHM} Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    # Host is set by the HTTP client from the URL; sending it explicitly is
    # redundant and httpx would duplicate it.
    signed.pop("host", None)
    return signed
