from __future__ import annotations

import ctypes
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess

from PIL import Image

from gamemanager.models import IconRebuildEntry, OperationReport
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.folder_icons import read_folder_rebuilt_flag, set_folder_rebuilt_flag
from gamemanager.services.icon_pipeline import build_multi_size_ico


PREVIEW_SIZES: tuple[int, ...] = (16, 24, 32, 48)


def _is_path_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def is_local_folder_icon(folder_path: Path, icon_path: Path) -> bool:
    if not folder_path.exists() or not folder_path.is_dir():
        return False
    if not icon_path.exists() or not icon_path.is_file():
        return False
    if icon_path.suffix.casefold() != ".ico":
        return False
    return _is_path_inside(icon_path, folder_path)


def collect_existing_local_icons(
    icon_targets: list[tuple[Path, Path]],
) -> tuple[OperationReport, list[IconRebuildEntry]]:
    report = OperationReport()
    entries: list[IconRebuildEntry] = []
    for folder_path, icon_path in icon_targets:
        if not is_local_folder_icon(folder_path, icon_path):
            report.skipped += 1
            report.details.append(f"{folder_path.name}: skipped (not a local .ico icon)")
            continue
        report.total += 1
        try:
            already_rebuilt = read_folder_rebuilt_flag(folder_path)
            summary = (
                "Already rebuilt (desktop.ini Rebuilt=true)."
                if already_rebuilt
                else "Not rebuilt yet (desktop.ini Rebuilt=false/missing)."
            )
            entries.append(
                IconRebuildEntry(
                    folder_path=str(folder_path),
                    icon_path=str(icon_path),
                    already_rebuilt=already_rebuilt,
                    summary=summary,
                )
            )
            report.succeeded += 1
            report.details.append(f"{folder_path.name}: {summary}")
        except Exception as exc:
            report.failed += 1
            report.details.append(f"{folder_path.name}: failed to inspect ({exc})")
    return report, entries


def _icon_available_sizes(icon_path: Path) -> list[int]:
    with Image.open(icon_path) as icon:
        sizes = icon.info.get("sizes") or set()
    unique = sorted(
        {
            int(width)
            for width, height in sizes
            if int(width) > 0 and int(height) > 0 and int(width) == int(height)
        }
    )
    return unique


def _icon_available_sizes_in_payload(payload: bytes) -> list[int]:
    with Image.open(BytesIO(payload)) as icon:
        sizes = icon.info.get("sizes") or set()
    unique = sorted(
        {
            int(width)
            for width, height in sizes
            if int(width) > 0 and int(height) > 0 and int(width) == int(height)
        }
    )
    return unique


def _nearest_size(target: int, available: list[int]) -> int:
    return min(available, key=lambda value: (abs(int(value) - int(target)), int(value)))


def _load_icon_frame(icon_path: Path, target_size: int) -> Image.Image:
    available = _icon_available_sizes(icon_path)
    if not available:
        with Image.open(icon_path) as icon:
            icon.load()
            frame = icon.convert("RGBA")
        if int(frame.width) != int(target_size):
            frame = frame.resize((target_size, target_size), Image.Resampling.LANCZOS)
        return frame

    source_size = int(target_size if target_size in available else _nearest_size(target_size, available))
    with Image.open(icon_path) as icon:
        icon.size = (source_size, source_size)
        icon.load()
        frame = icon.convert("RGBA")
    if source_size != target_size:
        frame = frame.resize((target_size, target_size), Image.Resampling.LANCZOS)
    return frame


def _load_icon_frame_from_payload(payload: bytes, target_size: int) -> Image.Image:
    available = _icon_available_sizes_in_payload(payload)
    if not available:
        with Image.open(BytesIO(payload)) as icon:
            icon.load()
            frame = icon.convert("RGBA")
        if int(frame.width) != int(target_size):
            frame = frame.resize((target_size, target_size), Image.Resampling.LANCZOS)
        return frame

    source_size = int(
        target_size if target_size in available else _nearest_size(target_size, available)
    )
    with Image.open(BytesIO(payload)) as icon:
        icon.size = (source_size, source_size)
        icon.load()
        frame = icon.convert("RGBA")
    if source_size != target_size:
        frame = frame.resize((target_size, target_size), Image.Resampling.LANCZOS)
    return frame


def _png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _run_attrib(arguments: list[str]) -> None:
    proc = subprocess.run(
        ["attrib", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or proc.stdout or "attrib failed").strip())


def _prepare_file_for_overwrite(path: Path) -> None:
    if not path.exists():
        return
    try:
        _run_attrib(["-r", "-s", "-h", str(path)])
    except OSError:
        pass
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def _shell_refresh(path: Path) -> None:
    try:
        shell32 = ctypes.windll.shell32
        SHCNE_UPDATEDIR = 0x00001000
        SHCNF_PATHW = 0x0005
        shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, str(path), None)
    except Exception:
        return


def _extract_largest_frame_png(icon_path: Path) -> bytes:
    available = _icon_available_sizes(icon_path)
    largest = int(max(available) if available else 0)
    with Image.open(icon_path) as icon:
        if largest > 0:
            icon.size = (largest, largest)
        icon.load()
        frame = icon.convert("RGBA")
    out = BytesIO()
    frame.save(out, format="PNG")
    return out.getvalue()


def _next_backup_path(icon_path: Path, stamp: str) -> Path:
    candidate = icon_path.with_name(f"{icon_path.stem}.gm_backup_{stamp}.ico")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        alt = icon_path.with_name(f"{icon_path.stem}.gm_backup_{stamp}_{index}.ico")
        if not alt.exists():
            return alt
        index += 1


def rebuild_existing_local_icons(
    entries: list[IconRebuildEntry],
    size_improvements: dict[int, dict[str, object]] | None = None,
    *,
    force_rebuild: bool = False,
    create_backups: bool = True,
    progress_cb=None,
    should_cancel=None,
) -> OperationReport:
    report = OperationReport(total=len(entries))
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    total = len(entries)
    if progress_cb is not None:
        progress_cb("Rebuild icons", 0, max(1, total))
    for idx, entry in enumerate(entries, start=1):
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("Icon rebuild canceled.")
        if progress_cb is not None:
            progress_cb("Rebuild icons", idx - 1, max(1, total))
        folder_path = Path(entry.folder_path)
        icon_path = Path(entry.icon_path)
        if not is_local_folder_icon(folder_path, icon_path):
            report.skipped += 1
            report.details.append(f"{folder_path.name}: skipped (not a local .ico icon)")
            continue

        already_rebuilt = read_folder_rebuilt_flag(folder_path)
        if already_rebuilt and not force_rebuild:
            report.skipped += 1
            report.details.append(f"{folder_path.name}: skipped (already rebuilt)")
            continue

        backup_path = _next_backup_path(icon_path, stamp) if create_backups else None
        try:
            source_png = _extract_largest_frame_png(icon_path)
            rebuilt = build_multi_size_ico(
                source_png,
                icon_style="none",
                bg_removal_engine="none",
                bg_removal_params={},
                text_preserve_config={"enabled": False, "method": "none"},
                border_shader={"enabled": False},
                size_improvements=size_improvements,
            )
            _prepare_file_for_overwrite(icon_path)
            if create_backups and backup_path is not None:
                shutil.copy2(icon_path, backup_path)
            icon_path.write_bytes(rebuilt)
            set_folder_rebuilt_flag(folder_path, True)
            _shell_refresh(folder_path)
            report.succeeded += 1
            if create_backups and backup_path is not None:
                report.details.append(
                    f"{folder_path.name}: rebuilt {icon_path.name} (backup: {backup_path.name})"
                )
            else:
                report.details.append(
                    f"{folder_path.name}: rebuilt {icon_path.name} (backup: disabled)"
                )
        except Exception as exc:
            report.failed += 1
            report.details.append(f"{folder_path.name}: rebuild failed ({exc})")
        if progress_cb is not None:
            progress_cb("Rebuild icons", idx, max(1, total))
    if progress_cb is not None:
        progress_cb("Rebuild icons", max(1, total), max(1, total))
    return report


def build_rebuild_preview_frames(
    entry: IconRebuildEntry,
    sizes: tuple[int, ...] = PREVIEW_SIZES,
    size_improvements: dict[int, dict[str, object]] | None = None,
) -> dict[int, tuple[bytes, bytes]]:
    folder_path = Path(entry.folder_path)
    icon_path = Path(entry.icon_path)
    if not is_local_folder_icon(folder_path, icon_path):
        raise ValueError("Not a local folder .ico icon")

    source_png = _extract_largest_frame_png(icon_path)
    rebuilt = build_multi_size_ico(
        source_png,
        icon_style="none",
        bg_removal_engine="none",
        bg_removal_params={},
        text_preserve_config={"enabled": False, "method": "none"},
        border_shader={"enabled": False},
        size_improvements=size_improvements,
    )
    frames: dict[int, tuple[bytes, bytes]] = {}
    for size in sizes:
        target_size = int(size)
        before_frame = _load_icon_frame(icon_path, target_size)
        after_frame = _load_icon_frame_from_payload(rebuilt, target_size)
        frames[target_size] = (_png_bytes(before_frame), _png_bytes(after_frame))
    return frames


def clean_backup_icon_files(root_paths: list[Path]) -> OperationReport:
    report = OperationReport()
    seen: set[str] = set()
    for root_path in root_paths:
        root = Path(root_path)
        if not root.exists() or not root.is_dir():
            report.skipped += 1
            report.details.append(f"{root}: skipped (missing root)")
            continue
        try:
            for candidate in root.rglob("*.ico"):
                name_cf = candidate.name.casefold()
                if ".gm_backup_" not in name_cf:
                    continue
                resolved = str(candidate.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                report.total += 1
                try:
                    _prepare_file_for_overwrite(candidate)
                    candidate.unlink()
                    report.succeeded += 1
                except Exception as exc:
                    report.failed += 1
                    report.details.append(f"{candidate}: delete failed ({exc})")
        except Exception as exc:
            report.failed += 1
            report.details.append(f"{root}: scan failed ({exc})")
    return report
