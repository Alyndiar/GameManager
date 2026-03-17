from __future__ import annotations

import re
from pathlib import Path


_TRAILING_VERSION_RE = re.compile(
    r"(?i)\s*(?:[-_ ]+)?(?:v\.?\d+(?:\.\d+)*|\d+(?:\.\d+)+|build\s*\d+)\s*$"
)
_WRAPPED_TAG_RE = re.compile(r"\s*[\(\[\{]\s*([^\]\)\}]+?)\s*[\)\]\}]\s*$")
_DELIM_TAG_RE = re.compile(r"\s*[-_]\s*([A-Za-z0-9][A-Za-z0-9 .&+'-]*)\s*$")
_TRAILING_DELIMS_RE = re.compile(r"[-_\s]+$")


def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_tag(value: str) -> str:
    cleaned = value.strip().strip("-_").strip()
    return collapse_whitespace(cleaned).casefold()


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


def _extract_one_suffix_tag(value: str) -> tuple[str, str, int] | None:
    wrapped = _WRAPPED_TAG_RE.search(value)
    if wrapped:
        observed = wrapped.group(1).strip()
        return observed, canonicalize_tag(observed), wrapped.start()
    delimited = _DELIM_TAG_RE.search(value)
    if delimited:
        observed = delimited.group(1).strip()
        return observed, canonicalize_tag(observed), delimited.start()
    return None


def extract_suffix_tags(value: str) -> list[tuple[str, str]]:
    text = value.strip()
    extracted: list[tuple[str, str]] = []
    while text:
        one = _extract_one_suffix_tag(text)
        if one is None:
            break
        observed, canonical, start = one
        if canonical:
            extracted.append((observed, canonical))
            text = text[:start].rstrip()
        else:
            break
    return extracted


def remove_approved_suffix_tags(value: str, approved_tags: set[str]) -> str:
    text = value.strip()
    while text:
        one = _extract_one_suffix_tag(text)
        if one is None:
            break
        _, canonical, start = one
        if canonical in approved_tags:
            text = text[:start].rstrip()
            text = _TRAILING_DELIMS_RE.sub("", text).rstrip()
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
    return collapse_whitespace(no_version)
