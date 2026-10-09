import re

# Basic UK postcode validator/normaliser.
# Returns the normalised postcode (e.g. "SW1A 1AA") on success, or False on failure.
# This implementation validates the essential structure: inward code must be digit + 2 letters,
# outward code must start with a letter and contain only letters/digits. It does not
# implement every historic exclusion rule but covers common and reported cases.

_INWARD_RE = re.compile(r"^\d[A-Z]{2}$")
_OUTWARD_RE = re.compile(r"^[A-Z][A-Z0-9]{1,3}$")


def validate_postcode(value):
    """Validate and normalise a UK postcode.

    Args:
        value: string input postcode (may be any case, with or without space)

    Returns:
        Normalised postcode like "SW1A 1AA" on success, or False on invalid input.
    """
    if not isinstance(value, str):
        return False

    cleaned = re.sub(r"\s+", "", value).upper()

    # length must be between 5 and 7 characters when space removed
    if len(cleaned) < 5 or len(cleaned) > 7:
        return False

    # basic allowed characters
    if not re.fullmatch(r"[A-Z0-9]+", cleaned):
        return False

    # inward code is last 3 characters: digit + 2 letters
    inward = cleaned[-3:]
    if not _INWARD_RE.match(inward):
        return False

    outward = cleaned[:-3]
    if not _OUTWARD_RE.match(outward):
        return False

    return outward + " " + inward
