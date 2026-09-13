from datetime import datetime, timedelta, timezone

from fpt.scheduler.limiter import RetailerLimiter, RetailerPolicy

START = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def _policy(**overrides) -> RetailerPolicy:
    defaults = dict(
        min_delay_s=10,
        jitter_s=5,
        max_requests_per_hour=90,
        max_requests_per_day=1200,
        reserved_share={"CONFIRM": 0.15, "HOT": 0.15},
    )
    defaults.update(overrides)
    return RetailerPolicy(**defaults)


def test_first_request_always_admitted():
    limiter = RetailerLimiter(_policy())
    admitted, reason = limiter.can_admit("BASELINE", now=START)
    assert admitted is True
    assert reason is None


def test_delay_floor_blocks_immediate_second_request():
    limiter = RetailerLimiter(_policy(min_delay_s=10))
    limiter.record_request(now=START)
    admitted, reason = limiter.can_admit("BASELINE", now=START + timedelta(seconds=3))
    assert admitted is False
    assert reason == "delay_not_elapsed"


def test_delay_floor_admits_after_elapsed():
    limiter = RetailerLimiter(_policy(min_delay_s=10))
    limiter.record_request(now=START)
    admitted, reason = limiter.can_admit("BASELINE", now=START + timedelta(seconds=11))
    assert admitted is True


def test_hourly_cap_enforced():
    limiter = RetailerLimiter(_policy(min_delay_s=0, jitter_s=0, max_requests_per_hour=2))
    limiter.record_request(now=START)
    limiter.record_request(now=START + timedelta(seconds=1))
    admitted, reason = limiter.can_admit("BASELINE", now=START + timedelta(seconds=2))
    assert admitted is False
    assert reason == "hourly_cap"


def test_hourly_cap_resets_on_new_hour():
    limiter = RetailerLimiter(_policy(min_delay_s=0, jitter_s=0, max_requests_per_hour=1))
    limiter.record_request(now=START)
    admitted, reason = limiter.can_admit("BASELINE", now=START + timedelta(hours=1, seconds=1))
    assert admitted is True


def test_non_reserve_kind_hits_daily_cap_before_reserve_eligible_kind():
    # 100 requests/day, 30% reserved for CONFIRM+HOT -> non-reserved cap = 70.
    policy = _policy(
        min_delay_s=0, jitter_s=0, max_requests_per_hour=1000, max_requests_per_day=100,
        reserved_share={"CONFIRM": 0.15, "HOT": 0.15},
    )
    limiter = RetailerLimiter(policy)
    limiter.state.requests_this_day = 70
    limiter.state.day_bucket_start = START

    baseline_admitted, baseline_reason = limiter.can_admit("BASELINE", now=START)
    assert baseline_admitted is False
    assert baseline_reason == "daily_cap"

    confirm_admitted, confirm_reason = limiter.can_admit("CONFIRM", now=START)
    assert confirm_admitted is True
    assert confirm_reason is None


def test_reserve_eligible_kind_still_capped_at_full_daily_max():
    policy = _policy(
        min_delay_s=0, jitter_s=0, max_requests_per_hour=1000, max_requests_per_day=100,
        reserved_share={"CONFIRM": 0.15, "HOT": 0.15},
    )
    limiter = RetailerLimiter(policy)
    limiter.state.requests_this_day = 100
    limiter.state.day_bucket_start = START

    admitted, reason = limiter.can_admit("CONFIRM", now=START)
    assert admitted is False
    assert reason == "daily_cap"


def test_next_delay_s_respects_floor_and_jitter_bounds():
    limiter = RetailerLimiter(_policy(min_delay_s=10, jitter_s=5))
    for _ in range(20):
        delay = limiter.next_delay_s()
        assert 10 <= delay <= 15
