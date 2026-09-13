from fpt.core.money import format_cents, parse_price_cents


def test_parses_dollar_sign_and_cents():
    assert parse_price_cents("$130.00") == 13000


def test_parses_bare_number():
    assert parse_price_cents("6.99") == 699


def test_parses_thousands_separator():
    assert parse_price_cents("$1,299.99") == 129999


def test_parses_whole_dollar_no_cents():
    assert parse_price_cents("$99") == 9900


def test_none_input_returns_none():
    assert parse_price_cents(None) is None


def test_empty_string_returns_none():
    assert parse_price_cents("") is None
    assert parse_price_cents("   ") is None


def test_unparseable_text_returns_none():
    assert parse_price_cents("Call for price") is None


def test_zero_price_returns_none_never_zero_as_missing():
    # A missing price must be None, never 0 (adapter contract rule).
    assert parse_price_cents("$0.00") is None


def test_format_cents_roundtrip():
    assert format_cents(13000) == "$130.00"
    assert format_cents(699) == "$6.99"
    assert format_cents(None) is None
