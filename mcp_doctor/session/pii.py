"""Deterministic PII detection on live tool responses.

Every detector requires a structural validity check on top of the pattern, so a random
digit string doesn't get flagged just because it's the right length:

- card: 13-19 digits (unseparated, or in real card grouping like 4-4-4-4), a real network prefix (Visa, Mastercard,
  Amex, Discover), and a valid Luhn checksum. The prefix check matters: Luhn alone passes
  1 in 10 random digit strings, including millisecond timestamps.
- ssn: US Social Security number in its dashed form only (123-45-6789), within issued
  ranges (area not 000, 666 or 900-999; group not 00; serial not 0000). Undashed nine-digit
  runs are too common (ids, zip+4) to flag.
- iban: country code, check digits, and a valid ISO 13616 mod-97 checksum.
- email: off by default. Docs, git metadata and support addresses put legitimate emails in
  many responses, so flagging them would mostly be noise. Enable it through the policy.

Findings are always masked (last 4 characters only). The point is to keep the value out of
the report, not to copy it somewhere new.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_DETECTORS = ("card", "ssn", "iban")
ALL_DETECTORS = ("card", "ssn", "iban", "email")

# A card candidate can't touch letters, digits, "_", "-" or a decimal point on either side. That
# rules out digit runs inside sha256 hashes, git commit ids and float fractions. A separated number
# must use real card grouping with one consistent separator, which rules out space-separated
# coordinates in SVG paths.
_CARD_RE = re.compile(
    r"(?<![0-9A-Za-z_.-])(?:"
    r"\d{13,19}"  # unseparated
    r"|\d{4}([ -])\d{4}\1\d{4}\1\d{1,4}(?:\1\d{3})?"  # 4-4-4-1..4 (13-16), 4-4-4-4-3 (19)
    r"|\d{4}([ -])\d{6}\2\d{5}"  # Amex 4-6-5
    r")(?![0-9A-Za-z_-])(?!\.\d)"
)
_SSN_RE = re.compile(r"(?<![0-9A-Za-z_.-])(\d{3})-(\d{2})-(\d{4})(?![0-9A-Za-z_-])(?!\.\d)")
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?)\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# ISO 13616 lengths for the countries most likely to show up; an IBAN whose length doesn't
# match its country is rejected even if the checksum happens to pass.
_IBAN_LENGTHS = {
    "AT": 20, "BE": 16, "CH": 21, "CZ": 24, "DE": 22, "DK": 18, "ES": 24, "FI": 18,
    "FR": 27, "GB": 22, "IE": 22, "IT": 27, "LU": 20, "NL": 18, "NO": 15, "PL": 28,
    "PT": 25, "SE": 24, "AE": 23, "SA": 24,
}


@dataclass(frozen=True)
class PiiFinding:
    kind: str
    masked: str

    def __str__(self) -> str:
        return f"{self.kind} {self.masked}"


def _mask(value: str) -> str:
    compact = re.sub(r"[ -]", "", value)
    return "*" * max(len(compact) - 4, 0) + compact[-4:]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_network_ok(digits: str) -> bool:
    n = len(digits)
    if digits[0] == "4":
        return n in (13, 16, 19)  # Visa
    two, four = int(digits[:2]), int(digits[:4])
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return n == 16  # Mastercard
    if two in (34, 37):
        return n == 15  # Amex
    if digits.startswith("6011") or two == 65 or 644 <= int(digits[:3]) <= 649:
        return 16 <= n <= 19  # Discover
    return False


def _ssn_ok(area: str, group: str, serial: str) -> bool:
    a = int(area)
    return a != 0 and a != 666 and a < 900 and group != "00" and serial != "0000"


def _iban_ok(raw: str) -> bool:
    iban = raw.replace(" ", "")
    expected = _IBAN_LENGTHS.get(iban[:2])
    if not expected or len(iban) != expected:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def find_pii(text: str, detectors: tuple[str, ...] | list[str] = DEFAULT_DETECTORS) -> list[PiiFinding]:
    unknown = set(detectors) - set(ALL_DETECTORS)
    if unknown:
        raise ValueError(f"unknown PII detector(s): {sorted(unknown)}; known: {list(ALL_DETECTORS)}")
    findings: list[PiiFinding] = []
    if "card" in detectors:
        for m in _CARD_RE.finditer(text):
            digits = re.sub(r"[ -]", "", m.group(0))
            if 13 <= len(digits) <= 19 and _card_network_ok(digits) and _luhn_ok(digits):
                findings.append(PiiFinding("card", _mask(m.group(0))))
    if "ssn" in detectors:
        for m in _SSN_RE.finditer(text):
            if _ssn_ok(*m.groups()):
                findings.append(PiiFinding("ssn", _mask(m.group(0))))
    if "iban" in detectors:
        for m in _IBAN_RE.finditer(text):
            if _iban_ok(m.group(1)):
                findings.append(PiiFinding("iban", _mask(m.group(1))))
    if "email" in detectors:
        for m in _EMAIL_RE.finditer(text):
            local, _, domain = m.group(0).partition("@")
            findings.append(PiiFinding("email", f"{local[:1]}***@{domain}"))
    return findings
