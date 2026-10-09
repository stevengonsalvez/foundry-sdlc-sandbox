"""
UK postcode normalisation and validation.

A UK postcode has the form:
    <outward code> <inward code>
    outward: AN, ANN, AAN, AANN, ANA, AANA  (A=letter, N=digit)
    inward:  NAA  (always)

Input handling: normalise_postcode upper-cases, strips surrounding whitespace and inserts
the space before the last three characters when none is present, so "sw1a1aa" becomes
"SW1A 1AA". A single space is expected; internal runs of spaces are not collapsed.

Gaps:
  * KNOWN GAP 1: validation is shape-only. The letter restrictions of the real scheme are
    not enforced (for example "Q1 1AA" is accepted although Q is never a first letter),
    and the special-case "GIR 0AA" is rejected.
    (Demo issue: tighten the letter sets and allow GIR 0AA.)
"""

import re

# Regex taken from UK government postcode format documentation (public domain).
_OUTWARD = r"[A-Z]{1,2}[0-9][0-9A-Z]?"
_INWARD = r"[0-9][A-Z]{2}"
_FULL_RE = re.compile(rf"^({_OUTWARD}) ({_INWARD})$")


def normalise_postcode(raw: str) -> str:
    """Return postcode in canonical 'AA9 9AA' form, uppercased, single space.

    Raises ValueError for clearly invalid input (wrong length after stripping).
    Does not validate the shape; use is_valid_postcode for that.
    """
    # Strip surrounding whitespace then collapse any internal whitespace so inputs like
    # " sw1a  1aa " or "sw1a1aa" are handled uniformly.
    stripped = raw.strip()
    # Collapse all whitespace characters to nothing so we can re-insert a single space
    # before the inward code.
    compact = re.sub(r"\s+", "", stripped)
    if len(compact) < 5 or len(compact) > 7:
        # UK postcodes without space are between 5 and 7 chars; with space they'd be 6-8.
        raise ValueError(f"Postcode length out of range: {raw!r}")
    upper = compact.upper()
    # Insert single space before the last 3 characters.
    normalised = upper[:-3] + " " + upper[-3:]
    return normalised


def is_valid_postcode(raw: str) -> bool:
    """Return True if raw is a syntactically valid UK postcode after normalisation.

    Accepts input with or without a separating space, any case.
    """
    try:
        normalised = normalise_postcode(raw)
    except ValueError:
        return False
    return bool(_FULL_RE.match(normalised))
