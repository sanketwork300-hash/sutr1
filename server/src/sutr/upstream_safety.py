import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeUpstreamUrlError(ValueError):
    pass


@dataclass(frozen=True)
class SafeUpstreamURL:
    raw_url: str
    parsed: SplitResult
    hostname: str
    port: int
    path: str
    resolved_ips: tuple[str, ...]


@dataclass(frozen=True)
class PinnedTarget:
    """A validated URL, pinned to the address that passed validation.

    `validate_safe_url` resolves a hostname and judges the addresses, but a
    client that then connects by hostname resolves it *again*, and a DNS name
    is free to answer differently the second time (rebinding). The pin closes
    that gap: the request goes to `url`, whose host is the vetted IP literal,
    while `headers` carries the original `Host` and `extensions` tells httpx
    to present and verify the original hostname during TLS (httpcore reads
    `sni_hostname` and passes it to `start_tls` as `server_hostname`, which is
    what the SNI extension and the certificate hostname check both use).
    """

    url: str
    hostname: str
    port: int
    address: str
    headers: dict[str, str]
    extensions: dict[str, Any]


# Ranges `ipaddress` does not flag (or flags only partially) that still reach
# something a tenant must not: carrier-grade NAT (cloud metadata lives at
# 100.100.100.200 on some providers) and NAT64 prefixes, through which an
# IPv6-only resolver can hand back an embedded IPv4 address.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries, if it carries one.

    Covers the mapped form (`::ffff:a.b.c.d`), the deprecated compatible form
    (`::a.b.c.d`) and the NAT64 well-known prefix (`64:ff9b::a.b.c.d`). All
    three are ways to write a blocked IPv4 address so that it no longer looks
    like one.
    """
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_WELL_KNOWN or (ip in _IPV4_COMPATIBLE and not ip.is_unspecified):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    # Judge the whole set of forms an address can take: a blocked IPv4 address
    # wrapped in IPv6 is still that address.
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            candidates.append(embedded)
    for candidate in candidates:
        # `is_global` is the primary test: anything not routable on the public
        # internet is not something a tenant gets to point this server at.
        if not candidate.is_global:
            return True
        # Belt and braces: the explicit ranges, in case a future `ipaddress`
        # reclassifies one of them (the CGNAT range has moved before).
        if (
            candidate.is_loopback
            or candidate.is_private
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_reserved
            or candidate.is_unspecified
            or any(candidate in network for network in _BLOCKED_NETWORKS)
        ):
            return True
    return False


def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    try:
        addrinfos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUpstreamUrlError("Host could not be resolved") from exc

    resolved = sorted({info[4][0] for info in addrinfos})
    if not resolved:
        raise UnsafeUpstreamUrlError("Host could not be resolved")
    blocked = [ip for ip in resolved if _blocked_ip(ip)]
    if blocked:
        raise UnsafeUpstreamUrlError("Host resolves to a blocked network range")
    return tuple(resolved)


def validate_safe_url(raw_url: str, *, allow_query: bool = True) -> SafeUpstreamURL:
    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise UnsafeUpstreamUrlError("URL is malformed") from exc

    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUpstreamUrlError("URL scheme must be http or https")
    if parsed.username or parsed.password:
        raise UnsafeUpstreamUrlError("URL must not include credentials")
    if not parsed.hostname:
        raise UnsafeUpstreamUrlError("URL host is required")
    if parsed.fragment:
        raise UnsafeUpstreamUrlError("URL fragments are not allowed")
    if parsed.query and not allow_query:
        raise UnsafeUpstreamUrlError("URL queries are not allowed here")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUpstreamUrlError("URL port is malformed") from exc

    if port <= 0 or port > 65535:
        raise UnsafeUpstreamUrlError("URL port is out of range")

    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname:
        raise UnsafeUpstreamUrlError("URL host is required")

    return SafeUpstreamURL(
        raw_url=raw_url,
        parsed=parsed,
        hostname=hostname,
        port=port,
        path=parsed.path or "/",
        resolved_ips=_resolve_host(hostname, port),
    )


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def pin_target(safe: SafeUpstreamURL) -> PinnedTarget:
    """Pin an already-validated URL to the first address that passed."""
    address = safe.resolved_ips[0]
    parsed = safe.parsed
    netloc = f"[{address}]" if ":" in address else address
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))

    # An IPv6 literal in a Host header is bracketed, like it is in a URL.
    host_header = f"[{safe.hostname}]" if ":" in safe.hostname else safe.hostname
    if safe.port != _default_port(parsed.scheme):
        host_header = f"{host_header}:{safe.port}"

    return PinnedTarget(
        url=url,
        hostname=safe.hostname,
        port=safe.port,
        address=address,
        headers={"Host": host_header},
        extensions={"sni_hostname": safe.hostname},
    )


def pinned_request_target(raw_url: str, *, allow_query: bool = True) -> PinnedTarget:
    """Validate `raw_url` and pin the request to the address that was vetted.

    This is the function every outbound fetch of a user-supplied URL should
    use: it resolves the hostname exactly once, rejects the URL if any answer
    is a blocked address, and returns what the caller needs to connect to one
    of the vetted addresses without resolving the name again. Send the request
    to `target.url`, merge `target.headers` into the request headers and pass
    `target.extensions` as the httpx request extensions.
    """
    return pin_target(validate_safe_url(raw_url, allow_query=allow_query))
