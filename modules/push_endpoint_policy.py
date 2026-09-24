"""Fail-closed network policy for browser Web Push provider endpoints.

Push subscriptions contain a provider URL supplied by the browser.  Treating
that URL as an ordinary outbound request would turn the notification worker
into an SSRF primitive.  This module validates the URL, rejects every DNS
answer that is not globally routable, and creates a requests session pinned to
one of the addresses that was actually approved.  TLS still uses the provider
hostname for SNI and certificate verification.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidURL


MAX_ENDPOINT_LENGTH = 2048
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class PushEndpointPolicyError(ValueError):
    """The endpoint cannot safely be contacted by the notification worker."""


@dataclass(frozen=True)
class ResolvedPushEndpoint:
    endpoint: str
    hostname: str
    authority: str
    addresses: tuple[str, ...]


Resolver = Callable[..., Iterable[tuple]]


def _canonical_endpoint(endpoint: object) -> tuple[str, str, str]:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise PushEndpointPolicyError("invalid_push_endpoint")
    if not endpoint.startswith("https://") or endpoint != endpoint.strip() or any(
        ord(character) <= 0x20 or ord(character) == 0x7F or character == "\\"
        for character in endpoint
    ):
        raise PushEndpointPolicyError("invalid_push_endpoint")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise PushEndpointPolicyError("invalid_push_endpoint") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise PushEndpointPolicyError("invalid_push_endpoint")

    hostname = parsed.hostname
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as error:
        raise PushEndpointPolicyError("invalid_push_endpoint") from error
    if hostname != hostname.lower() or hostname.endswith("."):
        raise PushEndpointPolicyError("invalid_push_endpoint")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        if len(hostname) > 253 or any(
            not _DNS_LABEL.fullmatch(label) for label in hostname.split(".")
        ):
            raise PushEndpointPolicyError("invalid_push_endpoint")
        authority_host = hostname
    else:
        authority_host = f"[{hostname}]" if literal.version == 6 else hostname

    authority = authority_host if port is None else f"{authority_host}:443"
    if parsed.netloc != authority:
        raise PushEndpointPolicyError("invalid_push_endpoint")
    canonical = urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))

    # requests is the eventual URL consumer.  Refuse URLs it would rewrite so
    # that parser differences cannot alter the destination after validation.
    prepared = requests.PreparedRequest()
    try:
        prepared.prepare_url(canonical, None)
    except requests.RequestException as error:
        raise PushEndpointPolicyError("invalid_push_endpoint") from error
    if prepared.url != canonical:
        raise PushEndpointPolicyError("invalid_push_endpoint")
    return canonical, hostname, authority


def _globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _globally_routable(address.ipv4_mapped)
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def resolve_push_endpoint(
    endpoint: object,
    *,
    resolver: Resolver | None = None,
) -> ResolvedPushEndpoint:
    """Validate and resolve an endpoint, rejecting mixed public/private DNS."""

    canonical, hostname, authority = _canonical_endpoint(endpoint)
    resolver = resolver or socket.getaddrinfo
    try:
        answers = resolver(
            hostname,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise PushEndpointPolicyError("unresolvable_push_endpoint") from error

    addresses: list[str] = []
    seen_addresses: set[str] = set()
    for answer in answers:
        try:
            raw_address = answer[4][0]
            address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        except (IndexError, TypeError, ValueError) as error:
            raise PushEndpointPolicyError("unresolvable_push_endpoint") from error
        if not _globally_routable(address):
            raise PushEndpointPolicyError("unsafe_push_endpoint")
        compressed = address.compressed
        if compressed not in seen_addresses:
            seen_addresses.add(compressed)
            addresses.append(compressed)
    if not addresses:
        raise PushEndpointPolicyError("unresolvable_push_endpoint")
    return ResolvedPushEndpoint(
        endpoint=canonical,
        hostname=hostname,
        authority=authority,
        addresses=tuple(addresses),
    )


class _PinnedHTTPSAdapter(HTTPAdapter):
    def __init__(self, resolved: ResolvedPushEndpoint):
        super().__init__(max_retries=0)
        self._resolved = resolved
        # A single approved address makes DNS rebinding between validation and
        # connect impossible. A later delivery performs a fresh validation.
        self._address = resolved.addresses[0]

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if request.url != self._resolved.endpoint or proxies:
            raise InvalidURL("push endpoint escaped its validated destination")
        _, pool_kwargs = self.build_connection_pool_key_attributes(
            request, verify, cert
        )
        pool_kwargs["assert_hostname"] = self._resolved.hostname
        pool_kwargs["server_hostname"] = self._resolved.hostname
        return self.poolmanager.connection_from_host(
            self._address,
            443,
            scheme="https",
            pool_kwargs=pool_kwargs,
        )


class PinnedPushSession(requests.Session):
    """A one-destination HTTPS session with redirects and proxy use disabled."""

    def __init__(self, resolved: ResolvedPushEndpoint):
        super().__init__()
        self._resolved = resolved
        self.trust_env = False
        self.proxies.clear()
        self.mount("https://", _PinnedHTTPSAdapter(resolved))

    def request(self, method, url, **kwargs):
        if method.upper() != "POST" or url != self._resolved.endpoint:
            raise PushEndpointPolicyError("push endpoint escaped its validated destination")
        kwargs["allow_redirects"] = False
        kwargs["verify"] = True
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Host"] = self._resolved.authority
        kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


def pinned_push_session(resolved: ResolvedPushEndpoint) -> PinnedPushSession:
    return PinnedPushSession(resolved)
