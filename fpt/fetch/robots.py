"""robots.txt checking and the retailer admission rule (architecture doc 7.1).

A retailer (or a single page type) is crawled only if all hold:
1. robots.txt allows the target paths for our UA token AND for `*`.
2. robots.txt does not disallow the whole site for any named AI/automated
   agent group (checked by the caller against a known-disallowed-group
   list; this module only evaluates path allow/disallow).
3. No challenge or block observed in the admission probe (fetch-layer
   concern, not this module's).
4. Pages are reachable by plain HTTP (data API retailers are exempt).

This module implements checks 1 and the mechanics for 2 (scanning group
names); checks 3/4 are enforced elsewhere (fetch layer / config).
"""

from __future__ import annotations

import hashlib
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

IDENTIFIED_USER_AGENT = "FishPriceBot/0.1 (+https://PRODUCT_DOMAIN/bot; bot@PRODUCT_DOMAIN)"

# A blanket disallow ("/") for any of these token groups is treated as the
# site stating its position toward automated agents -- we do not argue our
# token is technically different (architecture doc 7.1, item 2).
NAMED_AGENT_GROUPS_TO_RESPECT = (
    "GPTBot",
    "ClaudeBot",
    "Claude-Web",
    "anthropic-ai",
    "CCBot",
    "Google-Extended",
    "*",
)


@dataclass(frozen=True)
class RobotsCheck:
    allowed: bool
    reason: str | None
    robots_txt_sha256: str | None


def _fetch_robots_text(base_url: str, fetch_text) -> str | None:
    """`fetch_text` is a callable(url) -> str | None, injected so this
    module has no direct HTTP dependency (keeps it unit-testable without
    network access)."""
    robots_url = urljoin(base_url, "/robots.txt")
    return fetch_text(robots_url)


def check_admission(
    *,
    base_url: str,
    path: str,
    fetch_text,
    identified_user_agent: str = IDENTIFIED_USER_AGENT,
) -> RobotsCheck:
    """Evaluate admission rule items 1 and 2 for a single path.

    Returns allowed=False with a reason string the moment either check
    fails -- callers should treat that retailer/page-type as permanently
    excluded until config is changed by a human, not retried.
    """
    text = _fetch_robots_text(base_url, fetch_text)
    if text is None:
        # No robots.txt at all is conventionally "allow everything", but
        # for this project's politeness posture we require a resolvable
        # robots.txt before crawling anything -- fail closed.
        return RobotsCheck(allowed=False, reason="robots_txt_unreachable", robots_txt_sha256=None)

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    for group_name in NAMED_AGENT_GROUPS_TO_RESPECT:
        if _group_disallows_everything(text, group_name):
            return RobotsCheck(
                allowed=False,
                reason=f"whole_site_disallowed_for_group:{group_name}",
                robots_txt_sha256=digest,
            )

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())

    parsed = urlparse(base_url)
    full_url = f"{parsed.scheme}://{parsed.netloc}{path}"

    for ua in (identified_user_agent, "*"):
        if not parser.can_fetch(ua, full_url):
            return RobotsCheck(
                allowed=False,
                reason=f"disallowed_for_ua:{ua}",
                robots_txt_sha256=digest,
            )

    return RobotsCheck(allowed=True, reason=None, robots_txt_sha256=digest)


def _group_disallows_everything(robots_text: str, group_name: str) -> bool:
    """True if `group_name`'s User-agent block contains `Disallow: /` (or
    `Disallow: /*`), i.e. the whole site is blocked for that named agent.
    """
    current_group_matches = False
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            current_group_matches = value.lower() == group_name.lower()
            continue
        if key == "disallow" and current_group_matches:
            if value in ("/", "/*"):
                return True
    return False


def crawl_delay_seconds(robots_text: str, user_agent: str) -> float | None:
    """Return an explicit Crawl-delay for `user_agent`'s group if present."""
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_text.splitlines())
    try:
        delay = parser.crawl_delay(user_agent)
    except Exception:
        return None
    return float(delay) if delay is not None else None
