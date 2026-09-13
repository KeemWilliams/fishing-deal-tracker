"""Per-retailer rate limiting and request budgets (architecture doc 3.3 policy
block, 5.2 worker rules, 5.4 breaker).

Pure, in-memory, dependency-injected clock -- no DB, no sleeping -- so the
worker (or a test) can drive it deterministically. A real deployment wires
this to `retailers.policy` from `retailers.yaml` and persists counters
elsewhere (fpt/pipeline/store.py, owned alongside the DB layer); this
module only makes the admit/deny + wait-time decision.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

TaskKind = str  # "CONFIRM" | "HOT" | "DISCOVERY" | "ENROLL" | "BASELINE" | "CANARY"

# Reserve-eligible kinds may spend into a retailer's reserved daily share;
# everything else is SKIPPED_BUDGET once the non-reserved portion is spent
# (architecture doc 5.2, item 3).
RESERVE_ELIGIBLE_KINDS = frozenset({"CONFIRM", "HOT"})


@dataclass
class BreakerPolicy:
    consecutive_blocks: int = 5
    block_rate_threshold: float = 0.20
    block_rate_min_sample: int = 20
    cooldown_hours: int = 12
    disable_after_trips_in_48h: int = 2


@dataclass
class RetailerPolicy:
    min_delay_s: float
    jitter_s: float
    max_requests_per_hour: int
    max_requests_per_day: int
    reserved_share: dict[str, float] = field(default_factory=dict)  # e.g. {"CONFIRM": 0.15, "HOT": 0.15}
    breaker: BreakerPolicy = field(default_factory=BreakerPolicy)


@dataclass
class RetailerState:
    """Mutable counters for one retailer. Reset externally at hour/day
    boundaries by the caller (this module does not know wall-clock
    rollover policy beyond what `now` tells it on each call)."""

    last_request_at: datetime | None = None
    requests_this_hour: int = 0
    requests_this_day: int = 0
    hour_bucket_start: datetime | None = None
    day_bucket_start: datetime | None = None
    consecutive_blocks: int = 0
    recent_outcomes: list[bool] = field(default_factory=list)  # True = blocked
    breaker_open_until: datetime | None = None
    trips_in_48h: list[datetime] = field(default_factory=list)
    disabled: bool = False
    disabled_reason: str | None = None


class RetailerLimiter:
    def __init__(self, policy: RetailerPolicy, *, rng: random.Random | None = None):
        self.policy = policy
        self.state = RetailerState()
        self._rng = rng or random.Random()

    def _roll_buckets(self, now: datetime) -> None:
        if self.state.hour_bucket_start is None or now - self.state.hour_bucket_start >= timedelta(hours=1):
            self.state.hour_bucket_start = now
            self.state.requests_this_hour = 0
        if self.state.day_bucket_start is None or now - self.state.day_bucket_start >= timedelta(days=1):
            self.state.day_bucket_start = now
            self.state.requests_this_day = 0

    def can_admit(self, kind: TaskKind, *, now: datetime | None = None) -> tuple[bool, str | None]:
        """Returns (admitted, reason_if_denied). Reason is one of
        "breaker_open", "disabled", "delay_not_elapsed", "hourly_cap",
        "daily_cap" (SKIPPED_BUDGET in the architecture's task status enum)."""
        now = now or datetime.now(timezone.utc)
        self._roll_buckets(now)

        if self.state.disabled:
            return False, "disabled"
        if self.state.breaker_open_until and now < self.state.breaker_open_until:
            return False, "breaker_open"

        if self.state.last_request_at is not None:
            elapsed = (now - self.state.last_request_at).total_seconds()
            if elapsed < self.policy.min_delay_s:
                return False, "delay_not_elapsed"

        if self.state.requests_this_hour >= self.policy.max_requests_per_hour:
            return False, "hourly_cap"

        reserved_fraction = sum(self.policy.reserved_share.values())
        non_reserved_cap = self.policy.max_requests_per_day * (1 - reserved_fraction)
        if kind not in RESERVE_ELIGIBLE_KINDS and self.state.requests_this_day >= non_reserved_cap:
            return False, "daily_cap"
        if self.state.requests_this_day >= self.policy.max_requests_per_day:
            return False, "daily_cap"

        return True, None

    def next_delay_s(self) -> float:
        """Delay + jitter to wait before the *next* request, per 7.2 (floor
        plus jitter). Callers add this to `now` to compute `not_before`."""
        return self.policy.min_delay_s + self._rng.uniform(0, self.policy.jitter_s)

    def record_request(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._roll_buckets(now)
        self.state.last_request_at = now
        self.state.requests_this_hour += 1
        self.state.requests_this_day += 1

    def record_outcome(self, *, blocked: bool, now: datetime | None = None) -> None:
        """Feeds the breaker (architecture doc 5.4). Trips on 5 consecutive
        blocks, or a block rate over `block_rate_threshold` once at least
        `block_rate_min_sample` outcomes have been observed."""
        now = now or datetime.now(timezone.utc)
        policy = self.policy.breaker

        if blocked:
            self.state.consecutive_blocks += 1
        else:
            self.state.consecutive_blocks = 0

        self.state.recent_outcomes.append(blocked)
        if len(self.state.recent_outcomes) > policy.block_rate_min_sample:
            self.state.recent_outcomes.pop(0)

        should_trip = False
        if self.state.consecutive_blocks >= policy.consecutive_blocks:
            should_trip = True
        elif len(self.state.recent_outcomes) >= policy.block_rate_min_sample:
            rate = sum(self.state.recent_outcomes) / len(self.state.recent_outcomes)
            if rate > policy.block_rate_threshold:
                should_trip = True

        if should_trip and (self.state.breaker_open_until is None or now >= self.state.breaker_open_until):
            self.state.breaker_open_until = now + timedelta(hours=policy.cooldown_hours)
            self.state.trips_in_48h = [t for t in self.state.trips_in_48h if now - t < timedelta(hours=48)]
            self.state.trips_in_48h.append(now)
            self.state.consecutive_blocks = 0
            if len(self.state.trips_in_48h) >= policy.disable_after_trips_in_48h:
                self.state.disabled = True
                self.state.disabled_reason = "breaker_tripped_twice_in_48h"
