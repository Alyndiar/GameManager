from __future__ import annotations

import re
from pathlib import Path


_TRAILING_VERSION_RE = re.compile(
    r"(?i)\s*(?:[-_ ]+)?(?:v\.?\d+(?:\.\d+)*|\d+(?:\.\d+)+|build\s*\d+)\s*$"
)
_VERSION_TOKEN_RE = re.compile(
    r"(?i)^(?:v\.?\d+(?:\.\d+)*|\d+(?:\.\d+)+|build\s*\d+)$"
)
_NUMERIC_ONLY_RE = re.compile(r"^\d+$")
_NUMERIC_SERIES_RE = re.compile(r"^\d+(?:-\d+)+$")
_LEADING_NUMBER_RE = re.compile(r"^\d+")
_WRAPPED_TAG_RE = re.compile(r"\s*[\(\[\{]\s*([^\]\)\}]+?)\s*[\)\]\}]\s*$")
# Delimiter tags use - or _ directly before the suffix tag token.
# A space right after "-" (e.g. "- GOG") is intentionally ignored.
_DELIM_TAG_RE = re.compile(r"\s*[-_]([A-Za-z0-9][A-Za-z0-9 .&+']*)\s*$")
_TRAILING_DELIMS_RE = re.compile(r"[-_\s]+$")
_CLEANED_DASH_RE = re.compile(r"[-]+")


def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_tag(value: str) -> str:
    cleaned = value.strip().strip("-_").strip()
    return collapse_whitespace(cleaned).casefold()


def _looks_like_version_token(value: str) -> bool:
    return bool(_VERSION_TOKEN_RE.fullmatch(collapse_whitespace(value)))


def _is_numeric_only_token(value: str) -> bool:
    return bool(_NUMERIC_ONLY_RE.fullmatch(collapse_whitespace(value)))


def _has_numeric_series_prefix(text: str, start: int) -> bool:
    prefix = text[:start].strip()
    return bool(_NUMERIC_SERIES_RE.fullmatch(prefix))


def _is_number_series_suffix(prefix_text: str, observed: str) -> bool:
    prefix = prefix_text.rstrip()
    observed_clean = collapse_whitespace(observed)
    if not _LEADING_NUMBER_RE.match(observed_clean):
        return False
    return bool(prefix) and prefix[-1].isdigit()


def _wrapped_kind(value: str, start: int) -> str:
    for ch in value[start:]:
        if ch in "[{(":
            if ch == "[":
                return "wrapped_square"
            if ch == "{":
                return "wrapped_curly"
            return "wrapped_paren"
        if not ch.isspace():
            break
    return "wrapped_paren"


def _is_protected_dot(value: str, idx: int) -> bool:
    prev_char = value[idx - 1] if idx > 0 else ""
    next_char = value[idx + 1] if idx < len(value) - 1 else ""
    if prev_char.isdigit() and next_char.isdigit():
        return True
    if prev_char in ("v", "V") and next_char.isdigit():
        return True
    return False


def normalize_separators(value: str) -> str:
    chars: list[str] = []
    for idx, ch in enumerate(value):
        if ch == "_":
            chars.append(" ")
        elif ch == "." and not _is_protected_dot(value, idx):
            chars.append(" ")
        else:
            chars.append(ch)
    return collapse_whitespace("".join(chars))


def strip_trailing_versions(value: str) -> str:
    previous = value
    while True:
        updated = _TRAILING_VERSION_RE.sub("", previous).strip()
        if updated == previous:
            return collapse_whitespace(updated)
        previous = updated


def _extract_one_suffix_tag(value: str) -> tuple[str, str, int, str] | None:
    wrapped = _WRAPPED_TAG_RE.search(value)
    if wrapped:
        observed = wrapped.group(1).strip()
        return (
            observed,
            canonicalize_tag(observed),
            wrapped.start(),
            _wrapped_kind(value, wrapped.start()),
        )
    delimited = _DELIM_TAG_RE.search(value)
    if delimited:
        observed = delimited.group(1).strip()
        return observed, canonicalize_tag(observed), delimited.start(), "delimited"
    return None


def extract_suffix_tags(value: str) -> list[tuple[str, str]]:
    text = value.strip()
    extracted: list[tuple[str, str]] = []
    while text:
        one = _extract_one_suffix_tag(text)
        if one is None:
            break
        observed, canonical, start, kind = one
        if _is_numeric_only_token(observed):
            # Numeric-only suffixes are never tags.
            break
        if kind == "delimited" and _looks_like_version_token(observed):
            # Version suffixes (e.g. -v1.2) are not tags.
            break
        if kind == "delimited" and _has_numeric_series_prefix(text, start):
            # Names like 1-2-3-4 Full series are not tags.
            break
        if kind == "delimited" and _is_number_series_suffix(text[:start], observed):
            # Names like MGQ 1-3 English are not tags.
            break
        if canonical:
            extracted.append((observed, canonical))
            text = text[:start].rstrip()
            # If the trailing tag is wrapped ((), [], {}), do not parse anything earlier.
            if kind in {"wrapped_square", "wrapped_curly", "wrapped_paren"}:
                break
            # Delimited -tag extraction is single-pass to avoid consuming
            # non-tag hyphenated name segments.
            if kind == "delimited":
                break
        else:
            break
    return extracted


def remove_approved_suffix_tags(value: str, approved_tags: set[str]) -> str:
    text = value.strip()
    while text:
        one = _extract_one_suffix_tag(text)
        if one is None:
            break
        observed, canonical, start, kind = one
        if _is_numeric_only_token(observed):
            break
        if kind == "delimited" and _has_numeric_series_prefix(text, start):
            break
        if kind == "delimited" and _is_number_series_suffix(text[:start], observed):
            break
        if canonical in approved_tags:
            text = text[:start].rstrip()
            text = _TRAILING_DELIMS_RE.sub("", text).rstrip()
            if kind in {"wrapped_square", "wrapped_curly", "wrapped_paren"}:
                break
            continue
        break
    return collapse_whitespace(text)


def cleaned_name_from_full(
    full_name: str,
    is_file: bool,
    approved_tags: set[str],
) -> str:
    base = Path(full_name).stem if is_file else full_name
    normalized = normalize_separators(base)
    no_tags = remove_approved_suffix_tags(normalized, approved_tags)
    no_version = strip_trailing_versions(no_tags)
    no_dash = _CLEANED_DASH_RE.sub(" ", no_version)
    return collapse_whitespace(no_dash)
