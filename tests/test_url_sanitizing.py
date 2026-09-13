"""H1: URL sanitizing (fpt.adapters._shared.sanitize_offer_url /
load_allowed_hosts). Every case here reproduces one of the attack shapes
named in the security review: javascript:, data:, protocol-relative
//evil.com, plain http://, and a same-string-different-host absolute URL
(standing in for "redirect final_url on another host" -- see
test_pipeline_url_rejection.py for the full response.final_url scenario
against the real pipeline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fpt.adapters._shared import FALLBACK_ALLOWED_HOSTS, load_allowed_hosts, sanitize_offer_url

TW_HOSTS = FALLBACK_ALLOWED_HOSTS["tackle_warehouse"]
VALID_URL = "https://www.tacklewarehouse.com/product/123"
VALID_FALLBACK = "https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html"


class TestSanitizeOfferUrl:
    def test_valid_https_allowed_host_passes_through(self):
        assert sanitize_offer_url(VALID_URL, allowed_hosts=TW_HOSTS) == VALID_URL

    @pytest.mark.parametrize(
        "malicious",
        [
            "javascript:alert(document.cookie)",
            "data:text/html,<script>alert(1)</script>",
            "//evil.com/product/123",
            "http://www.tacklewarehouse.com/product/123",  # right host, wrong scheme
            "https://evil.com/product/123",  # right scheme, wrong host
            "https://www.tacklewarehouse.com.evil.com/product/123",  # host-confusion suffix attack
            "",
            None,
        ],
    )
    def test_malicious_or_invalid_candidate_falls_back(self, malicious):
        result = sanitize_offer_url(malicious, allowed_hosts=TW_HOSTS, fallback_url=VALID_FALLBACK)
        assert result == VALID_FALLBACK

    @pytest.mark.parametrize(
        "malicious",
        [
            "javascript:alert(1)",
            "data:text/html,x",
            "//evil.com/x",
            "http://www.tacklewarehouse.com/x",
            "https://evil.com/x",
        ],
    )
    def test_rejects_outright_when_fallback_also_unsafe(self, malicious):
        # "redirect final_url on another host" is exactly this shape: the
        # caller must never pass response.final_url as fallback_url, but if
        # a bad value somehow reaches this function as the fallback too,
        # the result is still None -- never a URL on the wrong host.
        assert sanitize_offer_url(malicious, allowed_hosts=TW_HOSTS, fallback_url="https://evil.com/redirected") is None

    def test_no_candidate_no_fallback_returns_none(self):
        assert sanitize_offer_url(None, allowed_hosts=TW_HOSTS, fallback_url=None) is None

    def test_empty_allowed_hosts_never_passes_anything(self):
        assert sanitize_offer_url(VALID_URL, allowed_hosts=frozenset(), fallback_url=VALID_URL) is None


class TestLoadAllowedHosts:
    def test_falls_back_to_local_map_when_config_missing_key(self, tmp_path: Path):
        config = tmp_path / "retailers.yaml"
        config.write_text("retailers:\n  - slug: tackle_warehouse\n    name: Tackle Warehouse\n", encoding="utf-8")
        hosts = load_allowed_hosts("tackle_warehouse", config_path=config)
        assert hosts == FALLBACK_ALLOWED_HOSTS["tackle_warehouse"]

    def test_reads_allowed_hosts_from_config_when_present(self, tmp_path: Path):
        config = tmp_path / "retailers.yaml"
        config.write_text(
            "retailers:\n"
            "  - slug: tackle_warehouse\n"
            "    allowed_hosts: [shop.tacklewarehouse.example]\n",
            encoding="utf-8",
        )
        hosts = load_allowed_hosts("tackle_warehouse", config_path=config)
        assert hosts == frozenset({"shop.tacklewarehouse.example"})

    def test_missing_config_file_falls_back(self, tmp_path: Path):
        hosts = load_allowed_hosts("academy", config_path=tmp_path / "does-not-exist.yaml")
        assert hosts == FALLBACK_ALLOWED_HOSTS["academy"]

    def test_unknown_retailer_returns_empty_set(self, tmp_path: Path):
        config = tmp_path / "retailers.yaml"
        config.write_text("retailers: []\n", encoding="utf-8")
        assert load_allowed_hosts("not_a_real_retailer", config_path=config) == frozenset()
