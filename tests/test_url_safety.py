"""Unit tests for fpt/fetch/url_safety.py (security review M3). No
network access anywhere -- every resolver is a fake."""

from __future__ import annotations

import pytest

from fpt.fetch.url_safety import (
    UnsafeUrlError,
    check_url_safety,
    fetch_safely,
    is_blocked_ip,
    registrable_domain,
    require_safe_url,
    same_registrable_domain,
)


def _public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def _private_resolver(host: str) -> list[str]:
    return ["10.0.0.5"]


class TestIsBlockedIp:
    @pytest.mark.parametrize(
        "ip",
        ["10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1"],
    )
    def test_blocks_private_and_reserved_ranges(self, ip):
        assert is_blocked_ip(ip) is True

    @pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "8.8.8.8"])
    def test_allows_public_addresses(self, ip):
        assert is_blocked_ip(ip) is False

    def test_unparseable_fails_closed(self):
        assert is_blocked_ip("not-an-ip") is True


class TestRegistrableDomain:
    def test_strips_subdomain(self):
        assert registrable_domain("www.tacklewarehouse.com") == "tacklewarehouse.com"

    def test_same_registrable_domain_true_for_subdomain_change(self):
        assert same_registrable_domain("www.tacklewarehouse.com", "cdn.tacklewarehouse.com")

    def test_same_registrable_domain_false_for_different_site(self):
        assert not same_registrable_domain("www.tacklewarehouse.com", "attacker.net")


class TestCheckUrlSafety:
    def test_off_allowlist_host_is_rejected(self):
        result = check_url_safety(
            "https://evil.example.com/x", allowed_hosts=["www.tacklewarehouse.com"], resolver=_public_resolver
        )
        assert result.allowed is False
        assert "host_not_allowlisted" in result.reason

    def test_plain_http_scheme_is_rejected(self):
        result = check_url_safety(
            "http://www.tacklewarehouse.com/x", allowed_hosts=["www.tacklewarehouse.com"], resolver=_public_resolver
        )
        assert result.allowed is False
        assert "scheme_not_https" in result.reason

    def test_private_ip_resolution_is_rejected(self):
        result = check_url_safety(
            "https://www.tacklewarehouse.com/x", allowed_hosts=["www.tacklewarehouse.com"], resolver=_private_resolver
        )
        assert result.allowed is False
        assert "blocked_ip" in result.reason

    def test_allowlisted_public_host_is_admitted(self):
        result = check_url_safety(
            "https://www.tacklewarehouse.com/x", allowed_hosts=["www.tacklewarehouse.com"], resolver=_public_resolver
        )
        assert result.allowed is True
        assert result.host == "www.tacklewarehouse.com"

    def test_dns_failure_fails_closed(self):
        def _boom(host):
            raise OSError("dns down")

        result = check_url_safety(
            "https://www.tacklewarehouse.com/x", allowed_hosts=["www.tacklewarehouse.com"], resolver=_boom
        )
        assert result.allowed is False
        assert "dns_resolution_failed" in result.reason

    def test_require_safe_url_raises_on_unsafe(self):
        with pytest.raises(UnsafeUrlError):
            require_safe_url(
                "https://evil.example.com/x", allowed_hosts=["www.tacklewarehouse.com"], resolver=_public_resolver
            )


class _FakeRequest:
    def __init__(self, url: str):
        self.url = url


class _FakeResponse:
    def __init__(self, final_url: str):
        self.final_url = final_url


class _RedirectingFetcher:
    """A Fetcher stand-in whose final_url lands on a DIFFERENT host than
    the request -- simulates a retailer page redirecting off-site."""

    def __init__(self, final_url: str):
        self._final_url = final_url

    def fetch(self, request, egress, user_agent, timeout_s):
        return _FakeResponse(self._final_url)


class TestFetchSafely:
    def test_rejects_unsafe_request_url_before_ever_calling_fetcher(self):
        called = {"n": 0}

        class _Spy:
            def fetch(self, *a, **k):
                called["n"] += 1
                return _FakeResponse("https://www.tacklewarehouse.com/x")

        with pytest.raises(UnsafeUrlError):
            fetch_safely(
                _Spy(), _FakeRequest("https://evil.example.com/x"), egress=None, user_agent="ua", timeout_s=1,
                allowed_hosts=["www.tacklewarehouse.com"], resolver=_public_resolver,
            )
        assert called["n"] == 0

    def test_redirect_to_a_different_registrable_domain_is_rejected(self):
        fetcher = _RedirectingFetcher("https://attacker.net/steal")
        with pytest.raises(UnsafeUrlError) as excinfo:
            fetch_safely(
                fetcher, _FakeRequest("https://www.tacklewarehouse.com/x"), egress=None, user_agent="ua", timeout_s=1,
                allowed_hosts=["www.tacklewarehouse.com", "attacker.net"], resolver=_public_resolver,
            )
        # even if attacker.net were (hypothetically) allowlisted, it is a
        # DIFFERENT registrable domain than the request started on
        assert "redirect_left_registrable_domain" in str(excinfo.value)

    def test_redirect_to_same_registrable_domain_is_allowed(self):
        fetcher = _RedirectingFetcher("https://www.tacklewarehouse.com/canonical")
        response = fetch_safely(
            fetcher, _FakeRequest("https://www.tacklewarehouse.com/x"), egress=None, user_agent="ua", timeout_s=1,
            allowed_hosts=["www.tacklewarehouse.com"], resolver=_public_resolver,
        )
        assert response.final_url == "https://www.tacklewarehouse.com/canonical"
