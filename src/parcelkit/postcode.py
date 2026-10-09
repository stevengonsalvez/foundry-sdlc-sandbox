"""
UK postcode normalisation and validation.

A UK postcode has the form:
    <outward code> <inward code>
    outward: AN, ANN, AAN, AANN, ANA, AANA  (A=letter, N=digit)
    inward:  NAA  (always)

Gaps:
  * KNOWN GAP 1: lowercase input with no space is not handled. For example
    "sw1a1aa" will fail validation even though "SW1A 1AA" is valid.
    Callers must upper-case and insert a space before calling normalise_postcode.
    (Demo issue: add auto-normalisation from any casing/spacing.)
"""

import re

# Regex taken from UK government postcode format documentation (public domain).
_OUTWARD = r"[A-Z]{1,2}[0-9][0-9A-Z]?"
_INWARD = r"[0-9][A-Z]{2}"
_FULL_RE = re.compile(rf"^({_OUTWARD}) ({_INWARD})$")


def normalise_postcode(raw: str) -> str:
    """Return postcode in canonical 'AA9 9AA' form, uppercased, single space.

    Raises ValueError for clearly invalid input (wrong length after stripping).
    Does NOT attempt to correct casing or missing spaces (see module docstring).
    """
    stripped = raw.strip()
    if len(stripped) < 5 or len(stripped) > 8:
        raise ValueError(f"Postcode length out of range: {raw!r}")
    upper = stripped.upper()
    # Insert space before the last 3 characters if none present.
    if " " not in upper:
        upper = upper[:-3] + " " + upper[-3:]
    return upper


def is_valid_postcode(raw: str) -> bool:
    """Return True if raw is a syntactically valid UK postcode after normalisation.

    Accepts input with or without a separating space, any case.
    """
    try:
        normalised = normalise_postcode(raw)
    except ValueError:
        return False
    return bool(_FULL_RE.match(normalised))
