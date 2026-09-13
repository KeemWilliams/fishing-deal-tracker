from fpt.core.normalize.keys import build_variant_key


def test_rod_variant_key_all_required_present():
    attrs = {"rod_type": "spinning", "length_in": 90, "power": "M", "action": "F", "pieces": 1}
    key = build_variant_key("rod", attrs)
    assert key == "rod_type=spinning|length_in=90|power=M|action=F|pieces=1"


def test_rod_variant_key_missing_action_returns_none():
    attrs = {"rod_type": "spinning", "length_in": 90, "power": "M", "action": None, "pieces": 1}
    assert build_variant_key("rod", attrs) is None


def test_other_category_never_auto_matches():
    assert build_variant_key("other", {"anything": "value"}) is None


def test_unknown_category_returns_none():
    assert build_variant_key("nonexistent_category", {}) is None
