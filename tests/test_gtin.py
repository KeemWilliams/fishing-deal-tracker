from fpt.core.gtin import normalize_all, normalize_gtin


def test_valid_gtin13_zero_padded_to_gtin14():
    # Real value observed live on a Tackle Warehouse product page
    # (St. Croix Triumph Spinning Rod, 2026-09-12).
    assert normalize_gtin("0780647104070") == "00780647104070"


def test_valid_gtin13_second_sample():
    assert normalize_gtin("0751981006382") == "00751981006382"


def test_placeholder_zero_gtin_rejected():
    # TW's used-gear rows frequently carry gtin13="0" (no barcode on file).
    assert normalize_gtin("0") is None


def test_invalid_checksum_rejected():
    # Flip the check digit of a known-valid GTIN.
    assert normalize_gtin("0780647104071") is None


def test_non_digit_junk_rejected():
    assert normalize_gtin("not-a-barcode") is None


def test_field_name_agnostic_extraction():
    # normalize_gtin takes only the attribute VALUE, never the field name
    # it came from -- callers extract the value before calling this. This
    # documents that a 13-digit value is handled identically regardless
    # of whether the HTML field that held it was named "gtin13" or (as
    # Academy does) "gtin8".
    assert normalize_gtin("0751981006382") == "00751981006382"


def test_normalize_all_drops_invalid_and_dedupes():
    result = normalize_all(["0780647104070", "0", "0780647104070", "garbage"])
    assert result == ["00780647104070"]
