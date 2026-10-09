"""Tests for parcelkit.postcode."""

import pytest

from parcelkit import is_valid_postcode, normalise_postcode
from parcelkit.tracking import _compute_check_digit
from parcelkit import is_valid_tracking_number


class TestNormalisePostcode:
    def test_already_normalised(self):
        assert normalise_postcode("SW1A 1AA") == "SW1A 1AA"

    def test_no_space_inserts_space(self):
        assert normalise_postcode("SW1A1AA") == "SW1A 1AA"

    def test_lowercase_uppercased(self):
        assert normalise_postcode("ec1a1bb") == "EC1A 1BB"

    def test_leading_trailing_whitespace_stripped(self):
        assert normalise_postcode("  W1A 1AA  ") == "W1A 1AA"

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            normalise_postcode("SW")

    def test_too_long_raises(self):
        with pytest.raises(ValueError):
            normalise_postcode("SW1A 1AA EXTRA")


class TestIsValidPostcode:
    @pytest.mark.parametrize("pc", [
        "SW1A 1AA",
        "W1A 1AA",
        "EC1A 1BB",
        "BS1 1AB",
        "B1 1AB",
        "N1 1AB",
        "CR2 6XH",
        "DN55 1PT",
    ])
    def test_valid_postcodes(self, pc):
        assert is_valid_postcode(pc) is True

    @pytest.mark.parametrize("pc", [
        "INVALID",
        "1W 1AA",     # starts with digit: outward must start with letter
        "SW1A 1A",    # inward code too short (only 2 chars)
        "A12 34AA",   # inward has two digits before letters
        "",
    ])
    def test_invalid_postcodes(self, pc):
        assert is_valid_postcode(pc) is False

    def test_no_space_valid(self):
        # normalise_postcode inserts space before last 3 chars
        assert is_valid_postcode("SW1A1AA") is True

    def test_lowercase_no_space(self):
        # normalise_postcode uppercases so "sw1a1aa" -> "SW1A 1AA" -> valid
        assert is_valid_postcode("sw1a1aa") is True

    def test_demo_gap_1_noted(self):
        # Lowercase input with a space is handled too (normalise_postcode upper-cases).
        assert is_valid_postcode("sw1a 1aa") is True


class TestIsValidTrackingNumber:
    def _make_tracking(self, body: str) -> str:
        cd = _compute_check_digit(body)
        return f"AB{body}{cd}GB"

    def test_valid_tracking_number(self):
        tn = self._make_tracking("12345678")
        assert is_valid_tracking_number(tn) is True

    def test_wrong_check_digit(self):
        body = "12345678"
        cd = _compute_check_digit(body)
        bad = f"AB{body}{(cd + 1) % 10}GB"
        assert is_valid_tracking_number(bad) is False

    def test_invalid_format(self):
        assert is_valid_tracking_number("NOTATRACK") is False

    def test_lowercase_cleaned(self):
        tn = self._make_tracking("87654321")
        assert is_valid_tracking_number(tn.lower()) is True

    def test_all_zeros_body_valid_with_correct_check(self):
        # KNOWN GAP 2 demo: all-zeros body check digit is 0; the number is valid.
        # Callers may want to reject placeholder all-zero bodies in future.
        tn = self._make_tracking("00000000")
        assert is_valid_tracking_number(tn) is True

    def test_too_short(self):
        assert is_valid_tracking_number("AB1234567") is False

    def test_too_long(self):
        assert is_valid_tracking_number("AB123456789012345") is False

    @pytest.mark.parametrize("body", ["11111111", "23456789", "99999999"])
    def test_various_bodies(self, body):
        tn = self._make_tracking(body)
        assert is_valid_tracking_number(tn) is True
