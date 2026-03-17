from __future__ import annotations

import re
from datetime import datetime
from typing import TypeAlias


_NATURAL_PARTS = re.compile(r"(\d+)")
NaturalToken: TypeAlias = tuple[int, int | str]


def natural_key(value: str) -> list[NaturalToken]:
    parts = _NATURAL_PARTS.split(value.casefold())
    key: list[NaturalToken] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return key


def sort_key_for_inventory(
    cleaned_name: str,
    full_name: str,
    modified_at: datetime,
) -> tuple[list[NaturalToken], list[NaturalToken], tuple[int, ...]]:
    modified_desc_key = (
        -modified_at.year,
        -modified_at.month,
        -modified_at.day,
        -modified_at.hour,
        -modified_at.minute,
        -modified_at.second,
        -modified_at.microsecond,
    )
    return (
        natural_key(cleaned_name),
        natural_key(full_name),
        modified_desc_key,
    )
