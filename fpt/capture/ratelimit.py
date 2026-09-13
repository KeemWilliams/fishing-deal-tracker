"""Trivial in-memory rate limiter for the capture ingest endpoint.

This is a personal-use, single-process MVP endpoint (see server.py) --
not a distributed service -- so an in-memory sliding window keyed by
client IP is sufficient and deliberately does not reach for Redis or any
external dependency. It resets on process restart, which is acceptable
here: the goal is to blunt a runaway or malfunctioning extension/script,
not to provide airtight multi-instance rate limiting.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, *, max_requests: int, window_s: float):
        self._max_requests = max_requests
        self._window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            cutoff = now - self._window_s
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
