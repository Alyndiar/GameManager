from __future__ import annotations

import ctypes
import os
import re
import subprocess
from configparser import ConfigParser
from pathlib import Path

from gamemanager.models import IconApplyResult


_INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*]+')


def _sanitize_icon_name(raw_name: str) -> str:
    normalized = _INVALID_FILE_CHARS.sub(" ", raw_name)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        normalized = "folder"
    return normalized


def detect_folder_icon_state(folder_path: Path) -> tuple[str, str | None, str | None]:
    if not folder_path.exists() or not folder_path.is_dir():
        return "none", None, None

    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return "none", None, None
    parser = ConfigParser(strict=False)
    try:
        raw = desktop_ini.read_bytes()
    except OSError:
        return "broken", None, str(desktop_ini)
    parsed = False
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            parser.read_string(raw.decode(encoding))
            parsed = True
            break
        except Exception:
            continue
    if not parsed:
        return "broken", None, str(desktop_ini)
    if not parser.has_section(".ShellClassInfo"):
        return "broken", None, str(desktop_ini)
    icon_resource = parser.get(".ShellClassInfo", "IconResource", fallback="").strip()
    if not icon_resource:
        return "broken", None, str(desktop_ini)
    icon_spec = icon_resource.split(",", 1)[0].strip().strip('"')
    if icon_spec.casefold().endswith(".ico.0"):
        icon_spec = icon_spec[:-2]
    if icon_spec.startswith(".\\"):
        icon_spec = icon_spec[2:]
    icon_path = (folder_path / icon_spec).resolve()
    if not icon_path.exists():
        return "broken", str(icon_path), str(desktop_ini)
    return "valid", str(icon_path), str(desktop_ini)


def _run_attrib(arguments: list[str]) -> None:
    proc = subprocess.run(
        ["attrib", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or proc.stdout or "attrib failed").strip())


def _shell_refresh(path: Path) -> None:
    try:
        shell32 = ctypes.windll.shell32
        SHCNE_UPDATEDIR = 0x00001000
        SHCNF_PATHW = 0x0005
        shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, str(path), None)
    except Exception:
        return


def apply_folder_icon(
    folder_path: Path,
    icon_bytes: bytes,
    icon_name_hint: str,
    info_tip: str | None = None,
) -> IconApplyResult:
    if not folder_path.exists() or not folder_path.is_dir():
        return IconApplyResult(
            folder_path=str(folder_path),
            status="failed",
            message="Folder does not exist.",
        )
    safe_name = _sanitize_icon_name(icon_name_hint)
    icon_path = folder_path / f"{safe_name}.ico"
    desktop_ini = folder_path / "desktop.ini"
    try:
        icon_path.write_bytes(icon_bytes)
        lines = [
            "[.ShellClassInfo]",
            f"IconResource=.\\{icon_path.name},0",
        ]
        if info_tip and info_tip.strip():
            lines.append(f"InfoTip={info_tip.strip()}")
        lines.append("Flags=0")
        desktop_ini.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

        _run_attrib(["+s", str(folder_path)])
        _run_attrib(["+s", "+h", str(desktop_ini)])
        _shell_refresh(folder_path)
    except OSError as exc:
        return IconApplyResult(
            folder_path=str(folder_path),
            status="failed",
            message=str(exc),
            ico_path=str(icon_path),
            desktop_ini_path=str(desktop_ini),
        )

    return IconApplyResult(
        folder_path=str(folder_path),
        status="applied",
        message="Icon applied.",
        ico_path=str(icon_path),
        desktop_ini_path=str(desktop_ini),
    )
