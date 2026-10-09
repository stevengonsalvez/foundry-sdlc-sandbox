"""
parcelkit: UK postcode normalisation/validation and parcel tracking-number check-digit validation.

Public API:
    normalise_postcode(raw: str) -> str
    is_valid_postcode(raw: str) -> bool
    is_valid_tracking_number(raw: str) -> bool
"""
from parcelkit.postcode import normalise_postcode, is_valid_postcode
from parcelkit.tracking import is_valid_tracking_number

__all__ = ["normalise_postcode", "is_valid_postcode", "is_valid_tracking_number"]
__version__ = "0.1.0"
