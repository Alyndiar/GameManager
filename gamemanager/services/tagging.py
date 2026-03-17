from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from gamemanager.models import TagCandidate
from gamemanager.services.normalization import extract_suffix_tags


def _name_for_tag_scan(full_name: str, is_file: bool) -> str:
    if is_file:
        return Path(full_name).stem
    return full_name


def collect_tag_candidates(
    names: list[tuple[str, bool]],
    non_tags: set[str],
) -> list[TagCandidate]:
    counter: dict[str, int] = defaultdict(int)
    observed: dict[str, str] = {}
    examples: dict[str, str] = {}
    for full_name, is_file in names:
        value = _name_for_tag_scan(full_name, is_file)
        suffixes = extract_suffix_tags(value)
        for observed_tag, canonical in suffixes:
            if not canonical or canonical in non_tags:
                continue
            counter[canonical] += 1
            observed.setdefault(canonical, observed_tag)
            examples.setdefault(canonical, full_name)
    now = datetime.now(timezone.utc).isoformat()
    result = [
        TagCandidate(
            canonical_tag=canonical,
            observed_tag=observed[canonical],
            count=count,
            example_name=examples[canonical],
            last_seen=now,
        )
        for canonical, count in counter.items()
    ]
    result.sort(key=lambda x: (x.canonical_tag, -x.count))
    return result

