"""PII detector: true positives on real-format values, and the false-positive shapes that
showed up when the first version was run over 20,513 real files (hash digits, SVG coordinates)."""

import pytest

from mcp_doctor.session.pii import find_pii


@pytest.mark.parametrize("text,kind", [
    ("card 4111 1111 1111 1111 on file", "card"),       # Visa test number, spaced
    ("4242424242424242", "card"),                         # Stripe test Visa, unseparated
    ("5555-5555-5555-4444", "card"),                      # Mastercard, dashed
    ("2223003122003222", "card"),                         # Mastercard 2-series
    ("3782 822463 10005", "card"),                        # Amex 4-6-5
    ("6011111111111117", "card"),                         # Discover
    ("SSN: 123-45-6789", "ssn"),
    ("GB82 WEST 1234 5698 7654 32", "iban"),
    ("DE89370400440532013000", "iban"),
])
def test_true_positives(text, kind):
    found = find_pii(text)
    assert [f.kind for f in found] == [kind]
    value = "".join(ch for ch in text.split(": ")[-1].replace(" on file", "").replace("card ", "") if ch.isalnum())
    assert found[0].masked == "*" * (len(value) - 4) + value[-4:]  # only the last 4 ever shown


@pytest.mark.parametrize("text", [
    "sha256: 9120596a4243005732070b1c",                  # digits inside a hash (was a false positive)
    "commit 1f783dd83a0363479638a4098117892927754",      # digits inside a git sha
    "M23128 831 23032 706 24420 156",                     # SVG path coordinates (was a false positive)
    "4111111111111112",                                   # right prefix, bad Luhn
    "1790353072000",                                      # millisecond timestamp
    "0.4242424242424242",                                 # float fraction
    "000-12-3456 666-12-3456 912-34-5678 123-00-4567 123-45-0000",  # SSNs outside issued ranges
    "123456789",                                          # undashed nine digits
    "2026-09-25 555-123-4567",                            # date, phone
    "GB82 WEST 1234 5698 7654 33",                        # IBAN, bad checksum
    "550e8400-e29b-41d4-a716-446655440000",               # UUID
])
def test_true_negatives(text):
    assert find_pii(text) == []


def test_email_is_off_by_default_and_masked_when_on():
    assert find_pii("contact support@example.com") == []
    found = find_pii("contact support@example.com", ["email"])
    assert [str(f) for f in found] == ["email s***@example.com"]


def test_unknown_detector_is_an_error():
    with pytest.raises(ValueError):
        find_pii("x", ["cards"])
