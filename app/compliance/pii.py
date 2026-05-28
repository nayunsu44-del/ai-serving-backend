from __future__ import annotations

import re
from collections.abc import Iterable

from app.normalized import NormalizedMessage


ENABLED_TYPES = {"rrn", "card", "phone", "email"}
DETECTOR_PRIORITY = ("email", "rrn", "card", "phone")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RRN_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})[-\s]?([1-8]\d{6})\b")
CARD_RE = re.compile(r"(?<!\d)(?:\d[- ]?){12,18}\d(?!\d)")

# This intentionally accepts common Korean mobile/landline forms, including +82
# forms without the leading 0. It may catch some non-phone numeric identifiers
# with phone-like grouping, but stricter validation would miss legitimate numbers.
PHONE_RE = re.compile(
    r"(?<!\d)(?:"
    r"(?:01[016789]|02|0\d{2})[-\s]?\d{3,4}[-\s]?\d{4}"
    r"|"
    r"\+82[-\s]?(?:1[016789]|2|\d{2})[-\s]?\d{3,4}[-\s]?\d{4}"
    r")(?!\d)"
)

_Candidate = tuple[int, int, str, str]


def _valid_rrn_date(month: str, day: str) -> bool:
    return 1 <= int(month) <= 12 and 1 <= int(day) <= 31


def _luhn_valid(value: str) -> bool:
    total = 0
    reverse_digits = [int(char) for char in reversed(value)]
    for index, digit in enumerate(reverse_digits):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _rrn_candidates(text: str) -> Iterable[_Candidate]:
    for match in RRN_RE.finditer(text):
        if not _valid_rrn_date(match.group(2), match.group(3)):
            continue
        yield (match.start(), match.end(), "rrn", match.group(0))


def _card_candidates(text: str) -> Iterable[_Candidate]:
    for match in CARD_RE.finditer(text):
        digits = re.sub(r"[- ]", "", match.group(0))
        if not 13 <= len(digits) <= 19:
            continue
        if not _luhn_valid(digits):
            continue
        yield (match.start(), match.end(), "card", match.group(0))


def _phone_candidates(text: str) -> Iterable[_Candidate]:
    for match in PHONE_RE.finditer(text):
        yield (match.start(), match.end(), "phone", match.group(0))


def _email_candidates(text: str) -> Iterable[_Candidate]:
    for match in EMAIL_RE.finditer(text):
        yield (match.start(), match.end(), "email", match.group(0))


DETECTORS = {
    "email": _email_candidates,
    "rrn": _rrn_candidates,
    "card": _card_candidates,
    "phone": _phone_candidates,
}


def _overlaps(candidate: _Candidate, accepted: list[_Candidate]) -> bool:
    candidate_start, candidate_end, _, _ = candidate
    return any(
        candidate_start < span_end and span_start < candidate_end
        for span_start, span_end, _, _ in accepted
    )


def _collect_candidates(text: str, enabled: set[str]) -> list[_Candidate]:
    accepted: list[_Candidate] = []
    for pii_type in DETECTOR_PRIORITY:
        if pii_type not in enabled:
            continue
        for candidate in DETECTORS[pii_type](text):
            if _overlaps(candidate, accepted):
                continue
            accepted.append(candidate)
    return accepted


def _apply_mask(
    text: str,
    candidates: list[_Candidate],
    counts: dict[str, int],
    next_indexes: dict[str, int],
    placeholders: dict[tuple[str, str], str],
) -> str:
    for _, _, pii_type, raw_value in sorted(candidates, key=lambda item: item[0]):
        counts[pii_type] = counts.get(pii_type, 0) + 1
        key = (pii_type, raw_value)
        if key in placeholders:
            continue
        index = next_indexes.get(pii_type, 0) + 1
        next_indexes[pii_type] = index
        placeholders[key] = f"[REDACTED:{pii_type.upper()}:{index}]"

    masked = text
    for start, end, pii_type, raw_value in sorted(
        candidates,
        key=lambda item: item[0],
        reverse=True,
    ):
        placeholder = placeholders[(pii_type, raw_value)]
        masked = masked[:start] + placeholder + masked[end:]

    return masked


def mask_text(text: str, enabled_types: Iterable[str]) -> tuple[str, dict[str, int]]:
    enabled = {pii_type for pii_type in enabled_types if pii_type in ENABLED_TYPES}
    if not enabled:
        return text, {}

    candidates = _collect_candidates(text, enabled)
    if not candidates:
        return text, {}

    counts: dict[str, int] = {}
    next_indexes: dict[str, int] = {}
    placeholders: dict[tuple[str, str], str] = {}
    return (
        _apply_mask(text, candidates, counts, next_indexes, placeholders),
        counts,
    )


def mask_messages(
    messages: list[NormalizedMessage],
    enabled_types: Iterable[str],
) -> tuple[list[NormalizedMessage], dict[str, int]]:
    enabled = {pii_type for pii_type in enabled_types if pii_type in ENABLED_TYPES}
    if not enabled:
        return [
            NormalizedMessage(role=message.role, content=message.content)
            for message in messages
        ], {}

    masked_messages: list[NormalizedMessage] = []
    counts: dict[str, int] = {}
    next_indexes: dict[str, int] = {}
    placeholders: dict[tuple[str, str], str] = {}

    for message in messages:
        candidates = _collect_candidates(message.content, enabled)
        masked_content = (
            _apply_mask(message.content, candidates, counts, next_indexes, placeholders)
            if candidates
            else message.content
        )
        masked_messages.append(
            NormalizedMessage(role=message.role, content=masked_content)
        )

    return masked_messages, counts
