from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import os
import time
from datetime import datetime
from pathlib import Path

from gamemanager.models import InventoryItem, RootDisplayInfo, RootFolder
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.folder_icons import detect_folder_icon_state
from gamemanager.services.normalization import cleaned_name_from_full
from gamemanager.services.scan_cache import DirectorySizeCache
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


def _directory_size_workers() -> int:
    # IO-bound walk: keep enough workers to overlap slow disk seeks, but bounded.
    cpu = os.cpu_count() or 8
    return max(2, min(32, cpu * 2))


def scan_roots(
    roots: list[RootFolder],
    approved_tags: set[str],
    root_infos_by_id: dict[int, RootDisplayInfo] | None = None,
    progress_cb=None,
    should_cancel=None,
    dir_size_cache: DirectorySizeCache | None = None,
    size_workers: int | None = None,
    progress_interval_s: float = 0.05,
) -> list[InventoryItem]:
    def _emit_progress(stage: str, current: int, total: int, *, force: bool = False) -> None:
        if progress_cb is None:
            return
        nonlocal last_progress_emit
        now = time.monotonic()
        if not force and (now - last_progress_emit) < max(0.01, progress_interval_s) and current < total:
            return
        last_progress_emit = now
        try:
            progress_cb(stage, current, total)
        except Exception:
            return

    root_info = dict(root_infos_by_id or {})
    scan_ts = datetime.now()
    items: list[InventoryItem] = []
    pending_dir_sizes: list[tuple[InventoryItem, Path]] = []
    last_progress_emit = 0.0
    total_roots = len(roots)
    scanned_roots = 0
    total_children = 0

    root_children: list[tuple[RootFolder, RootDisplayInfo, list[Path]]] = []
    for root in roots:
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("Scan canceled")
        root_path = Path(root.path)
        if not root_path.exists() or not root_path.is_dir():
            scanned_roots += 1
            _emit_progress("Scanning roots", scanned_roots, max(1, total_roots))
            continue
        info = root_info.get(root.id)
        if info is None:
            info = get_root_display_info(root)
            root_info[root.id] = info
        children: list[Path] = []
        try:
            children = list(root_path.iterdir())
        except OSError:
            children = []
        total_children += len(children)
        root_children.append((root, info, children))
        scanned_roots += 1
        _emit_progress("Scanning roots", scanned_roots, max(1, total_roots))

    scanned_children = 0
    _emit_progress("Scanning entries", 0, max(1, total_children), force=True)
    for root, info, children in root_children:
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("Scan canceled")
        for child in children:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("Scan canceled")
            try:
                stat = child.stat()
            except OSError:
                scanned_children += 1
                if scanned_children % 25 == 0 or scanned_children == total_children:
                    _emit_progress("Scanning entries", scanned_children, max(1, total_children))
                continue
            is_dir = child.is_dir()
            full_name = child.name
            cleaned_name = cleaned_name_from_full(
                full_name=full_name,
                is_file=not is_dir,
                approved_tags=approved_tags,
            )
            cached_size: int | None = None
            icon_status = "none"
            folder_icon_path: str | None = None
            desktop_ini_path: str | None = None
            info_tip = ""
            if is_dir:
                (
                    icon_status,
                    folder_icon_path,
                    desktop_ini_path,
                    info_tip,
                ) = detect_folder_icon_state(child)
                cached_size = None
                if dir_size_cache is not None:
                    cached_size = dir_size_cache.get(child, stat.st_mtime_ns)
            item = InventoryItem(
                root_id=root.id,
                root_path=root.path,
                source_label=info.source_label,
                full_name=full_name,
                full_path=str(child),
                is_dir=is_dir,
                extension=child.suffix.casefold() if child.is_file() else "",
                size_bytes=cached_size if (is_dir and cached_size is not None) else (0 if is_dir else stat.st_size),
                created_at=datetime.fromtimestamp(stat.st_ctime),
                modified_at=datetime.fromtimestamp(stat.st_mtime),
                cleaned_name=cleaned_name,
                scan_ts=scan_ts,
                icon_status=icon_status,
                folder_icon_path=folder_icon_path,
                desktop_ini_path=desktop_ini_path,
                info_tip=info_tip,
            )
            items.append(item)
            if is_dir and cached_size is None:
                pending_dir_sizes.append((item, child))
            scanned_children += 1
            if scanned_children % 25 == 0 or scanned_children == total_children:
                _emit_progress("Scanning entries", scanned_children, max(1, total_children))

    if pending_dir_sizes:
        workers_raw = _directory_size_workers() if size_workers is None else max(1, int(size_workers))
        workers = min(workers_raw, len(pending_dir_sizes))
        done_sizes = 0
        total_sizes = len(pending_dir_sizes)
        _emit_progress("Computing folder sizes", 0, total_sizes, force=True)
        pool = ThreadPoolExecutor(max_workers=workers)
        future_meta = {
            pool.submit(_directory_size_bytes, path): (item, path)
            for item, path in pending_dir_sizes
        }
        pending = set(future_meta.keys())
        try:
            while pending:
                if should_cancel is not None and should_cancel():
                    for fut in pending:
                        fut.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise OperationCancelled("Scan canceled")
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    item, path = future_meta[future]
                    try:
                        size_value = int(future.result())
                    except Exception:
                        size_value = 0
                    item.size_bytes = size_value
                    if dir_size_cache is not None:
                        try:
                            mtime_ns = path.stat().st_mtime_ns
                        except OSError:
                            mtime_ns = 0
                        if mtime_ns > 0:
                            dir_size_cache.put(path, mtime_ns, size_value)
                    done_sizes += 1
                    if done_sizes % 10 == 0 or done_sizes == total_sizes:
                        _emit_progress("Computing folder sizes", done_sizes, total_sizes)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    items.sort(
        key=lambda x: sort_key_for_inventory(
            cleaned_name=x.cleaned_name,
            full_name=x.full_name,
            modified_at=x.modified_at,
        )
    )
    _emit_progress("Sorting results", 1, 1, force=True)
    return items
