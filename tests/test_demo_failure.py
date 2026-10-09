from parcelkit import is_valid_tracking_number


def test_tracking_number_with_letter_suffix_is_valid():
    # Deliberate demo failure for the CI triage agent.
    assert is_valid_tracking_number("AB123456785GB") is True
