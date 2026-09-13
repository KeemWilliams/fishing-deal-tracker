from datetime import datetime, timedelta, timezone

from fpt.scheduler.limiter import BreakerPolicy, RetailerLimiter, RetailerPolicy

START = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def _limiter(**breaker_overrides) -> RetailerLimiter:
    breaker = BreakerPolicy(
        consecutive_blocks=5,
        block_rate_threshold=0.20,
        block_rate_min_sample=20,
        cooldown_hours=12,
        disable_after_trips_in_48h=2,
    )
    for key, value in breaker_overrides.items():
        setattr(breaker, key, value)
    policy = RetailerPolicy(
        min_delay_s=0, jitter_s=0, max_requests_per_hour=1000, max_requests_per_day=1000,
        breaker=breaker,
    )
    return RetailerLimiter(policy)


def test_five_consecutive_blocks_trips_breaker():
    limiter = _limiter()
    for i in range(5):
        limiter.record_outcome(blocked=True, now=START + timedelta(minutes=i))
    assert limiter.state.breaker_open_until is not None
    admitted, reason = limiter.can_admit("BASELINE", now=START + timedelta(minutes=5))
    assert admitted is False
    assert reason == "breaker_open"


def test_breaker_reopens_after_cooldown():
    limiter = _limiter(cooldown_hours=1)
    for i in range(5):
        limiter.record_outcome(blocked=True, now=START + timedelta(minutes=i))
    admitted, _ = limiter.can_admit("BASELINE", now=START + timedelta(hours=2))
    assert admitted is True


def test_non_consecutive_blocks_do_not_trip_below_threshold():
    limiter = _limiter()
    for i in range(4):
        limiter.record_outcome(blocked=True, now=START + timedelta(minutes=i))
    limiter.record_outcome(blocked=False, now=START + timedelta(minutes=4))
    assert limiter.state.consecutive_blocks == 0
    assert limiter.state.breaker_open_until is None


def test_block_rate_over_threshold_trips_without_five_consecutive():
    limiter = _limiter(consecutive_blocks=100)  # disable the consecutive path
    # 5 blocked, 15 ok, interleaved -> rate 25% > 20% threshold, sample=20
    outcomes = ([True, False, False, False] * 5)[:20]
    for i, blocked in enumerate(outcomes):
        limiter.record_outcome(blocked=blocked, now=START + timedelta(minutes=i))
    assert limiter.state.breaker_open_until is not None


def test_two_trips_in_48h_disables_retailer():
    limiter = _limiter(cooldown_hours=1, disable_after_trips_in_48h=2)
    # First trip
    for i in range(5):
        limiter.record_outcome(blocked=True, now=START + timedelta(minutes=i))
    assert limiter.state.disabled is False

    # Cooldown passes, second trip within 48h
    second_start = START + timedelta(hours=2)
    for i in range(5):
        limiter.record_outcome(blocked=True, now=second_start + timedelta(minutes=i))

    assert limiter.state.disabled is True
    assert limiter.state.disabled_reason == "breaker_tripped_twice_in_48h"


def test_trips_more_than_48h_apart_do_not_disable():
    limiter = _limiter(cooldown_hours=1, disable_after_trips_in_48h=2)
    for i in range(5):
        limiter.record_outcome(blocked=True, now=START + timedelta(minutes=i))

    far_start = START + timedelta(hours=60)
    for i in range(5):
        limiter.record_outcome(blocked=True, now=far_start + timedelta(minutes=i))

    assert limiter.state.disabled is False
