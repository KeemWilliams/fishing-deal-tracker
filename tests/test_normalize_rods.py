from fpt.core.normalize.rods import (
    normalize_rod_attributes,
    parse_length_in,
    parse_power_action,
)


def test_parse_length_feet_and_inches():
    assert parse_length_in("7'6\"") == 90


def test_parse_length_feet_only():
    assert parse_length_in("7'") == 84


def test_parse_length_unparseable_returns_none():
    assert parse_length_in("Medium") is None
    assert parse_length_in(None) is None


def test_parse_power_action_combined_style():
    power, action = parse_power_action("Medium - Fast")
    assert power == "M"
    assert action == "F"


def test_parse_power_action_unmapped_returns_none_never_guessed():
    power, action = parse_power_action("Bananas - Oranges")
    assert power is None
    assert action is None


def test_normalize_rod_attributes_from_tw_style_list():
    style_items = {
        "Spinning Rod Length": "7'6\"",
        "Spinning Rod Power - Spinning Rod Taper": "Medium - Fast",
    }
    attrs = normalize_rod_attributes(style_items)
    assert attrs["length_in"] == 90
    assert attrs["power"] == "M"
    assert attrs["action"] == "F"
    assert attrs["rod_type"] == "spinning"
    assert attrs["pieces"] == 1


def test_normalize_rod_attributes_missing_action_stays_none_for_review():
    style_items = {"Spinning Rod Length": "7'6\""}
    attrs = normalize_rod_attributes(style_items)
    assert attrs["length_in"] == 90
    assert attrs["power"] is None
    assert attrs["action"] is None
