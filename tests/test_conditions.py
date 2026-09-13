from fpt.core.conditions import map_condition
from fpt.core.models import Condition

CONDITION_MAP = {
    "tackle_warehouse": {
        "new": "NEW",
        "excellent": "USED_LIKE_NEW",
        "very good": "USED_VERY_GOOD",
    }
}


def test_maps_known_wording_case_insensitive():
    condition, raw = map_condition(
        "tackle_warehouse", "Excellent", condition_map=CONDITION_MAP
    )
    assert condition == Condition.USED_LIKE_NEW
    assert raw == "Excellent"


def test_unmapped_wording_returns_none_not_a_guess():
    condition, raw = map_condition(
        "tackle_warehouse", "Mint", condition_map=CONDITION_MAP
    )
    assert condition is None
    assert raw == "Mint"


def test_absent_wording_defaults_to_new_when_requested():
    condition, raw = map_condition(
        "tackle_warehouse", None, condition_map=CONDITION_MAP, default_new=True
    )
    assert condition == Condition.NEW
    assert raw is None


def test_absent_wording_stays_none_on_used_pages():
    condition, raw = map_condition(
        "tackle_warehouse", None, condition_map=CONDITION_MAP, default_new=False
    )
    assert condition is None
