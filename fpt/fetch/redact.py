"""Secret redaction for anything that gets logged or printed (security
review M8): a connection string, a proxy URL, or an exception's `str()`
can carry `user:pass@host` or `password=...`/`api_key=...`-style
credentials -- e.g. `DatabaseUnavailable`'s message embeds the raw
`DATABASE_URL` on a connection failure, and a PROXY egress failure embeds
whatever `FPT_PROXY_URL_<RETAILER>` resolved to. Every place in this
codebase's fetch/scheduler/CLI layer that turns an exception into a
string for the tick summary or stdout must pass it through `redact()`
first.
"""

from __future__ import annotations

import re

# user:pass@ in any URL (http://, https://, postgres://, ...)
_USERINFO_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")
# key=value fragments for common credential field names, query-string or
# space-separated (covers "password=hunter2", "?api_key=abc&", "PWD=x;").
_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token|access[_-]?key)\s*=\s*[^&\s;]+"
)


def redact(text: str | None) -> str:
    if not text:
        return "" if text is None else text
    redacted = _USERINFO_RE.sub("://[REDACTED]@", text)
    redacted = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
    return redacted


def redact_exception(exc: BaseException) -> str:
    return redact(str(exc))
