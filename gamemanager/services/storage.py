from __future__ import annotations

import ctypes
import os
import re
import shutil
from pathlib import Path

import psutil

from gamemanager.models import RootDisplayInfo, RootFolder


_MOUNT_BASE = Path(r"E:\Mount")
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:\\?$")


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def mountpoint_sort_key(mountpoint: str) -> str:
    return _norm_path(mountpoint)


def _resolve_mountpoint(path: Path) -> str:
    target = _norm_path(str(path))
    best = str(path.anchor or path)
    best_len = -1
    for part in psutil.disk_partitions(all=True):
        mount = part.mountpoint
        if not mount:
            continue
        mount_norm = _norm_path(mount)
        if target == mount_norm or target.startswith(mount_norm + os.sep):
            if len(mount_norm) > best_len:
                best = mount
                best_len = len(mount_norm)
    return best


def _volume_label(volume_root: str) -> str:
    root_path = volume_root
    if not root_path.endswith("\\"):
        root_path += "\\"
    volume_name_buffer = ctypes.create_unicode_buffer(261)
    file_system_name_buffer = ctypes.create_unicode_buffer(261)
    serial_number = ctypes.c_ulong()
    max_component_len = ctypes.c_ulong()
    file_system_flags = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root_path),
        volume_name_buffer,
        ctypes.sizeof(volume_name_buffer),
        ctypes.byref(serial_number),
        ctypes.byref(max_component_len),
        ctypes.byref(file_system_flags),
        file_system_name_buffer,
        ctypes.sizeof(file_system_name_buffer),
    )
    if ok:
        return volume_name_buffer.value or root_path.rstrip("\\")
    return root_path.rstrip("\\")


def _mount_name_under_base(path_value: Path) -> str | None:
    path_parts = path_value.parts
    base_parts = _MOUNT_BASE.parts
    if len(path_parts) <= len(base_parts):
        return None
    matches_base = all(
        os.path.normcase(path_parts[idx].rstrip("\\/"))
        == os.path.normcase(base_parts[idx].rstrip("\\/"))
        for idx in range(len(base_parts))
    )
    if not matches_base:
        return None
    return path_parts[len(base_parts)]


def source_label_for_path(root_path: Path, mountpoint: str) -> str:
    root_mount_name = _mount_name_under_base(root_path)
    if root_mount_name:
        return root_mount_name
    mount_mount_name = _mount_name_under_base(Path(mountpoint))
    if mount_mount_name:
        return mount_mount_name
    if _DRIVE_LETTER_RE.match(mountpoint):
        return mountpoint[:2].upper()
    clean_mount = mountpoint.rstrip("\\/")
    return Path(clean_mount).name or clean_mount


def get_root_display_info(root: RootFolder) -> RootDisplayInfo:
    root_path = Path(root.path)
    mountpoint = _resolve_mountpoint(root_path)
    source_label = source_label_for_path(root_path, mountpoint)
    # For managed mount roots, drive_name must be the mount folder name.
    # Prefer root-path derivation in case mountpoint resolution falls back to drive letter.
    mount_name = _mount_name_under_base(root_path) or _mount_name_under_base(
        Path(mountpoint)
    )
    if mount_name:
        drive_name = mount_name
    else:
        drive_name = _volume_label(mountpoint)
    total_size = 0
    free_space = 0
    try:
        # Query from configured root path so mounted-volume free space is reported,
        # not the host drive free space.
        usage = shutil.disk_usage(root_path)
        total_size = usage.total
        free_space = usage.free
    except (FileNotFoundError, OSError):
        try:
            usage = shutil.disk_usage(mountpoint)
            total_size = usage.total
            free_space = usage.free
        except (FileNotFoundError, OSError):
            total_size = 0
            free_space = 0
    return RootDisplayInfo(
        root_id=root.id,
        root_path=root.path,
        source_label=source_label,
        drive_name=drive_name,
        total_size_bytes=total_size,
        free_space_bytes=free_space,
        mountpoint=mountpoint,
    )
