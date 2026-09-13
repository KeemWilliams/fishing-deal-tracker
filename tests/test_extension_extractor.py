"""Smoke test for the browser extension's pure extraction function
(extension/lib/extractor.js), run against hand-written HTML fixtures via
Node -- no browser, no DOM library, no live requests to any retailer.

Skips (does not fail) when `node` isn't on PATH, matching the project's
"never fail the suite over an optional tool" convention (see
tests/integration/conftest.py's DATABASE_URL skip).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "_browser_capture"
DRIVER = FIXTURES_DIR / "run_extractor.js"
ACADEMY_FIXTURE = Path(__file__).parent / "fixtures" / "academy" / "product_single_variant_instoreonly.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run_extractor(html_file: Path, page_url: str = "", was_price_regex_source: str | None = None) -> dict | None:
    args = ["node", str(DRIVER), str(html_file), page_url]
    if was_price_regex_source:
        args.append(was_price_regex_source)
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node driver failed: {result.stderr}"
    return json.loads(result.stdout)


def test_scheels_fixture_extracts_current_and_was_price():
    """Scheels hand-written fixture: JSON-LD gives current price + title +
    condition; the site-specific wasPriceRegex (mirrored here, since the
    driver takes it as an arg rather than requiring site-configs.js to be
    Node-loadable) recovers the strikethrough price."""
    html_file = FIXTURES_DIR / "scheels_product.html"
    was_price_regex = r'class="[^"]*strike-through-price[^"]*"[^>]*>\s*\$?\(?([\d,]+\.\d{2})\)?'

    result = _run_extractor(
        html_file,
        page_url="https://www.scheels.com/p/st-croix-triumph-spinning-rod/123456.html",
        was_price_regex_source=was_price_regex,
    )

    assert result is not None
    assert result["title_raw"] == "St. Croix Triumph Spinning Rod"
    assert result["current_price_cents"] == 6497
    assert result["was_price_cents"] == 12999
    assert result["currency"] == "USD"
    assert result["condition"] == "NEW"
    assert result["product_url"] == "https://www.scheels.com/p/st-croix-triumph-spinning-rod/123456.html"


def test_scheels_fixture_without_site_config_still_gets_current_price():
    """No wasPriceRegex supplied -> JSON-LD-only extraction still works,
    just without the was-price. Proves the two extraction layers are
    independent."""
    html_file = FIXTURES_DIR / "scheels_product.html"
    result = _run_extractor(html_file, page_url="https://www.scheels.com/p/x.html")
    assert result is not None
    assert result["current_price_cents"] == 6497
    assert "was_price_cents" not in result or result.get("was_price_cents") is None


def test_academy_fixture_extracts_from_shared_real_fixture():
    """Reuses the SAME fixture the adapter test suite already commits at
    tests/fixtures/academy/ (see extension/lib/site-configs.js's docstring
    for why Academy has no wasPriceRegex: its product JSON-LD carries only
    the current price per the architecture doc's adapter matrix)."""
    assert ACADEMY_FIXTURE.exists(), "expected the existing academy adapter fixture to be present"
    result = _run_extractor(
        ACADEMY_FIXTURE, page_url="https://www.academy.com/p/shimano-stradic-fl-spinning-reel"
    )
    assert result is not None
    assert result["title_raw"] == "Shimano Stradic FL Spinning Reel"
    assert result["current_price_cents"] == 18997
    assert result["currency"] == "USD"
    assert result["condition"] == "NEW"
    assert result.get("was_price_cents") is None


def test_page_with_no_json_ld_returns_null():
    html_file = FIXTURES_DIR / "no_product_data.html"
    html_file.write_text("<html><body><p>Nothing here.</p></body></html>", encoding="utf-8")
    try:
        result = _run_extractor(html_file)
        assert result is None
    finally:
        html_file.unlink()


def test_malformed_json_ld_does_not_crash_and_returns_null():
    html_file = FIXTURES_DIR / "broken_json_ld.html"
    html_file.write_text(
        '<html><body><script type="application/ld+json">{not valid json</script></body></html>',
        encoding="utf-8",
    )
    try:
        result = _run_extractor(html_file)
        assert result is None
    finally:
        html_file.unlink()
