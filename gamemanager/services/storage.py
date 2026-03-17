from __future__ import annotations

import ctypes
import os
import re
import shutil
from pathlib import Path

import psutil

from gamemanager.models import RootDisplayInfo, RootFolder


_MOUNT_BASE = Path(r"E:\Mounts")
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:\\?$")


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


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


def source_label_for_path(root_path: Path, mountpoint: str) -> str:
    root_norm = Path(_norm_path(str(root_path)))
    mount_base_norm = Path(_norm_path(str(_MOUNT_BASE)))
    try:
        relative = root_norm.relative_to(mount_base_norm)
        if relative.parts:
            return relative.parts[0]
    except ValueError:
        pass
    if _DRIVE_LETTER_RE.match(mountpoint):
        return mountpoint[:2].upper()
    clean_mount = mountpoint.rstrip("\\/")
    return Path(clean_mount).name or clean_mount


def get_root_display_info(root: RootFolder) -> RootDisplayInfo:
    root_path = Path(root.path)
    mountpoint = _resolve_mountpoint(root_path)
    source_label = source_label_for_path(root_path, mountpoint)
    drive_name = _volume_label(mountpoint)
    free_space = 0
    try:
        free_space = shutil.disk_usage(mountpoint).free
    except (FileNotFoundError, OSError):
        free_space = 0
    return RootDisplayInfo(
        root_id=root.id,
        root_path=root.path,
        source_label=source_label,
        drive_name=drive_name,
        free_space_bytes=free_space,
        mountpoint=mountpoint,
    )

