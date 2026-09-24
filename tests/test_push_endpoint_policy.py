import socket

import pytest
import requests

from modules.push_endpoint_policy import (
    PushEndpointPolicyError,
    pinned_push_session,
    resolve_push_endpoint,
)


def _answers(*addresses):
    def resolver(host, port, *, family, type, proto):
        assert host
        assert port == 443
        assert family == socket.AF_UNSPEC
        assert type == socket.SOCK_STREAM
        assert proto == socket.IPPROTO_TCP
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443, 0, 0) if ":" in address else (address, 443),
            )
            for address in addresses
        ]

    return resolver


def test_resolve_push_endpoint_canonicalizes_and_accepts_only_public_answers():
    resolved = resolve_push_endpoint(
        "https://push.example.test:443/path/token?key=value",
        resolver=_answers("8.8.8.8", "2001:4860:4860::8888"),
    )
    assert resolved.endpoint == "https://push.example.test:443/path/token?key=value"
    assert resolved.hostname == "push.example.test"
    assert resolved.authority == "push.example.test:443"
    assert resolved.addresses == ("8.8.8.8", "2001:4860:4860::8888")

    root = resolve_push_endpoint(
        "https://push.example.test", resolver=_answers("8.8.4.4")
    )
    assert root.endpoint == "https://push.example.test/"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://push.example.test/path",
        "HTTPS://push.example.test/path",
        "https://user@push.example.test/path",
        "https://user:password@push.example.test/path",
        "https://push.example.test:444/path",
        "https://push.example.test/path#fragment",
        "https://PUSH.example.test/path",
        "https://push.example.test./path",
        "https://push.example.test\\@127.0.0.1/path",
        " https://push.example.test/path",
        "https://push.example.test/path\n",
        "https://-push.example.test/path",
        "https://push..example.test/path",
        "",
        None,
    ],
)
def test_resolve_push_endpoint_rejects_ambiguous_or_non_https_urls(endpoint):
    with pytest.raises(PushEndpointPolicyError):
        resolve_push_endpoint(endpoint, resolver=_answers("8.8.8.8"))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
def test_resolve_push_endpoint_rejects_non_global_addresses(address):
    with pytest.raises(PushEndpointPolicyError):
        resolve_push_endpoint(
            "https://push.example.test/path", resolver=_answers(address)
        )


def test_resolve_push_endpoint_rejects_mixed_public_private_dns_and_empty_dns():
    with pytest.raises(PushEndpointPolicyError):
        resolve_push_endpoint(
            "https://push.example.test/path",
            resolver=_answers("8.8.8.8", "127.0.0.1"),
        )
    with pytest.raises(PushEndpointPolicyError):
        resolve_push_endpoint(
            "https://push.example.test/path", resolver=_answers()
        )


def test_pinned_session_connects_to_approved_ip_with_original_tls_identity(monkeypatch):
    resolved = resolve_push_endpoint(
        "https://push.example.test/path", resolver=_answers("8.8.8.8")
    )
    session = pinned_push_session(resolved)
    adapter = session.get_adapter(resolved.endpoint)
    prepared = requests.Request("POST", resolved.endpoint).prepare()
    pool = adapter.get_connection_with_tls_context(
        prepared, verify=True, proxies={}, cert=None
    )

    assert session.trust_env is False
    assert pool.host == "8.8.8.8"
    assert pool.port == 443
    assert pool.assert_hostname == "push.example.test"
    assert pool.conn_kw["server_hostname"] == "push.example.test"

    calls = []

    def redirect_response(request, **kwargs):
        calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 307
        response.headers["Location"] = "https://127.0.0.1/private"
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(adapter, "send", redirect_response)
    response = session.post(resolved.endpoint, data=b"encrypted")
    assert response.status_code == 307
    assert len(calls) == 1
    assert calls[0][0].headers["Host"] == "push.example.test"
    assert calls[0][1]["verify"] is True

    with pytest.raises(PushEndpointPolicyError):
        session.post("https://127.0.0.1/private", data=b"encrypted")
    with pytest.raises(PushEndpointPolicyError):
        session.get(resolved.endpoint)
    session.close()
