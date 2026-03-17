from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from gamemanager.models import InventoryItem, RootDisplayInfo, RootFolder
from gamemanager.services.normalization import cleaned_name_from_full
from gamemanager.services.sorting import sort_key_for_inventory
from gamemanager.services.storage import get_root_display_info


def list_root_display_infos(roots: list[RootFolder]) -> list[RootDisplayInfo]:
    return [get_root_display_info(root) for root in roots]


def _directory_size_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def scan_roots(
    roots: list[RootFolder],
    approved_tags: set[str],
) -> list[InventoryItem]:
    root_info = {root.id: get_root_display_info(root) for root in roots}
    scan_ts = datetime.now()
    items: list[InventoryItem] = []
    for root in roots:
        root_path = Path(root.path)
        if not root_path.exists() or not root_path.is_dir():
            continue
        info = root_info[root.id]
        for child in root_path.iterdir():
            try:
                stat = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            full_name = child.name
            cleaned_name = cleaned_name_from_full(
                full_name=full_name,
                is_file=not is_dir,
                approved_tags=approved_tags,
            )
            items.append(
                InventoryItem(
                    root_id=root.id,
                    root_path=root.path,
                    source_label=info.source_label,
                    full_name=full_name,
                    full_path=str(child),
                    is_dir=is_dir,
                    extension=child.suffix.casefold() if child.is_file() else "",
                    size_bytes=_directory_size_bytes(child) if is_dir else stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_ctime),
                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                    cleaned_name=cleaned_name,
                    scan_ts=scan_ts,
                )
            )
    items.sort(
        key=lambda x: sort_key_for_inventory(
            cleaned_name=x.cleaned_name,
            full_name=x.full_name,
            modified_at=x.modified_at,
        )
    )
    return items
