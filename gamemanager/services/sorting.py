from __future__ import annotations

import re
from datetime import datetime
from typing import Any


_NATURAL_PARTS = re.compile(r"(\d+)")


def natural_key(value: str) -> list[Any]:
    parts = _NATURAL_PARTS.split(value.casefold())
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return key


def sort_key_for_inventory(
    cleaned_name: str,
    full_name: str,
    modified_at: datetime,
) -> tuple[list[Any], list[Any], float]:
    return (
        natural_key(cleaned_name),
        natural_key(full_name),
        -modified_at.timestamp(),
    )

