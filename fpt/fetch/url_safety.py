"""SSRF guard (security review finding M3): every URL this codebase ever
turns into an outbound request -- a configured discovery/product page, an
adapter-parsed `next_page_url`, or an enrolled `DiscoveredItem.product_url`
-- must be checked BEFORE the request is made, and the response's
`final_url` must be checked AGAIN after redirects, since a retailer page
(or a compromised/misbehaving one) is untrusted input that can embed any
URL it likes.

Checks enforced (all must pass):
  1. scheme is exactly "https" (no plain http, no file://, no gopher://, ...)
  2. the hostname is on that retailer's configured `allowed_hosts` list
     (config/retailers.yaml) -- an exact, case-insensitive match, not a
     substring/suffix check (a substring check on "tacklewarehouse.com"
     would also admit "evil-tacklewarehouse.com.attacker.net")
  3. every IP address the hostname resolves to is a routable, public
     unicast address -- not loopback, link-local, private (RFC 1918),
     CGNAT (RFC 6598), multicast, reserved, or unspecified

`resolver` is injected (defaults to real DNS via `socket.getaddrinfo`) so
callers -- and every test in this codebase -- never need real network
access to exercise this module; a test supplies a fake resolver that
returns whatever address it wants to prove is admitted or blocked.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlparse

MAX_REDIRECTS = 5

# RFC 1918 private ranges, RFC 6598 CGNAT, loopback, link-local, benchmarking
# (RFC 2544), documentation ranges, and multicast/reserved space. `ipaddress`
# already flags loopback/link-local/multicast/reserved/unspecified via its
# own properties; this list covers the ranges that are NOT already private
# per `is_private` in every Python version this project targets (3.14) plus
# a couple of belt-and-suspenders duplicates for clarity at the call site.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT (RFC 6598)
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "::/128",
        "64:ff9b::/96",  # NAT64 well-known prefix (can carry private IPv4)
        "100::/64",  # discard-only prefix
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "::ffff:0:0/96",  # IPv4-mapped IPv6 -- unwrapped below, kept as a backstop
    )
)

Resolver = Callable[[str], Iterable[str]]


class UnsafeUrlError(Exception):
    def __init__(self, reason: str, url: str):
        self.reason = reason
        self.url = url
        super().__init__(f"{reason}: {url}")


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    reason: str | None
    host: str | None


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return list({info[4][0] for info in infos})


def _unwrap_ipv4_mapped(ip: "ipaddress.IPv6Address | ipaddress.IPv4Address"):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable address -- fail closed
    ip = _unwrap_ipv4_mapped(ip)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip.is_private:
        return True
    for network in _BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return True
    return False


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a public suffix list: last two
    dot-separated labels. Correct for every retailer host in this project
    (all plain .com domains); a multi-part TLD (co.uk, com.au, ...) would
    be handled incorrectly -- flagged as a known limitation, not currently
    reachable since every configured `allowed_hosts` entry is a .com
    hostname."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 2:
        return host.lower()
    return ".".join(labels[-2:])


def same_registrable_domain(host_a: str, host_b: str) -> bool:
    return registrable_domain(host_a) == registrable_domain(host_b)


def check_url_safety(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    resolver: Resolver | None = None,
) -> SafetyResult:
    resolver = resolver or default_resolver
    parsed = urlparse(url)

    if parsed.scheme != "https":
        return SafetyResult(False, f"scheme_not_https:{parsed.scheme or '(none)'}", None)

    host = parsed.hostname
    if not host:
        return SafetyResult(False, "no_host", None)

    allowed_lower = {h.lower() for h in allowed_hosts}
    if host.lower() not in allowed_lower:
        return SafetyResult(False, f"host_not_allowlisted:{host}", host)

    try:
        addrs = list(resolver(host))
    except Exception as exc:  # noqa: BLE001 - any DNS failure fails closed
        return SafetyResult(False, f"dns_resolution_failed:{type(exc).__name__}", host)

    if not addrs:
        return SafetyResult(False, "dns_no_addresses", host)

    for addr in addrs:
        if is_blocked_ip(addr):
            return SafetyResult(False, f"blocked_ip:{addr}", host)

    return SafetyResult(True, None, host)


def require_safe_url(url: str, *, allowed_hosts: Iterable[str], resolver: Resolver | None = None) -> str:
    """Returns the validated hostname, or raises `UnsafeUrlError`."""
    result = check_url_safety(url, allowed_hosts=allowed_hosts, resolver=resolver)
    if not result.allowed:
        raise UnsafeUrlError(result.reason or "unsafe_url", url)
    return result.host  # type: ignore[return-value]


def fetch_safely(
    fetcher,
    request,
    egress,
    user_agent: str,
    timeout_s: float,
    *,
    allowed_hosts: Iterable[str],
    resolver: Resolver | None = None,
):
    """Wraps any `Fetcher.fetch(...)` call (fpt/fetch/fetcher.py Protocol)
    with the pre- and post-request SSRF checks (architecture review M3).
    Raises `UnsafeUrlError` and never calls the underlying fetcher at all
    if the REQUEST url is unsafe; raises the same if the fetch's own
    `final_url` (after any redirects `HttpFetcher`/scrapling followed)
    lands on a different host, a non-allowlisted host, a private IP, or a
    different registrable domain than the request started on -- a
    same-host redirect (e.g. `www.` to a bare apex, or an https-only
    canonicalization) is allowed as long as it stays within the allowlist
    and registrable domain; a redirect to an entirely different site
    (attacker-controlled or otherwise) is not.
    """
    origin_host = require_safe_url(request.url, allowed_hosts=allowed_hosts, resolver=resolver)
    response = fetcher.fetch(request, egress, user_agent, timeout_s)
    final_host = require_safe_url(response.final_url, allowed_hosts=allowed_hosts, resolver=resolver)
    if not same_registrable_domain(origin_host, final_host):
        raise UnsafeUrlError("redirect_left_registrable_domain", response.final_url)
    return response
