import pytest
from src.postcode import validate_postcode


def test_lowercase_no_space_normalised():
    assert validate_postcode("sw1a1aa") == "SW1A 1AA"


def test_valid_with_space():
    assert validate_postcode("SW1A 1AA") == "SW1A 1AA"


def test_invalid_short():
    assert validate_postcode("A1 1AA") is False or validate_postcode("A11AA") is False


def test_invalid_inward():
    assert validate_postcode("SW1A ABC") is False or validate_postcode("SW1AABC") is False
