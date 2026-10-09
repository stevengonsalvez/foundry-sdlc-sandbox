"""
Parcel tracking-number check-digit validation.

Tracking numbers follow a synthetic scheme for demo purposes:
    Format: 2 alpha prefix + 8 digits + 1 check digit + 2 alpha suffix
    Example: AB12345678CGB

Check-digit algorithm (Luhn-like mod-10 over the 8 digit body):
    1. Sum all 8 digits, doubling every other digit (positions 1, 3, 5, 7 from left,
       0-indexed: 0, 2, 4, 6). If the doubled value > 9, subtract 9.
    2. check_digit = (10 - (total % 10)) % 10
    3. The provided check character (position 10) must equal str(check_digit).

Gaps:
  * KNOWN GAP 2: an all-zero numeric body ("00000000") has check digit 0, and nothing
    rejects it, so is_valid_tracking_number("AB000000000GB") returns True. That may be
    undesirable for placeholder numbers.
    (Demo issue: add an all-zeros guard.)
"""

import re

_TRACKING_RE = re.compile(r"^([A-Z]{2})(\d{8})(\d)([A-Z]{2})$")


def _compute_check_digit(body: str) -> int:
    """Compute Luhn-like mod-10 check digit for an 8-digit body string."""
    total = 0
    for i, ch in enumerate(body):
        digit = int(ch)
        if i % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - (total % 10)) % 10


def is_valid_tracking_number(raw: str) -> bool:
    """Return True if raw is a syntactically valid tracking number with correct check digit.

    Expected format: 2 alpha + 8 digits + 1 check digit + 2 alpha (total 13 chars).
    Input is uppercased and stripped before matching.
    """
    cleaned = raw.strip().upper()
    match = _TRACKING_RE.match(cleaned)
    if not match:
        return False
    body = match.group(2)
    provided_check = int(match.group(3))
    return _compute_check_digit(body) == provided_check
