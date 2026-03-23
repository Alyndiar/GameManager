from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from configparser import ConfigParser
from pathlib import Path

from gamemanager.models import OperationReport, RootFolder


def _run_attrib(arguments: list[str]) -> None:
    proc = subprocess.run(
        ["attrib", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or proc.stdout or "attrib failed").strip())


def _shell_refresh(_path: Path) -> None:
    try:
        shell32 = ctypes.windll.shell32
        SHCNE_UPDATEDIR = 0x00001000
        SHCNF_PATHW = 0x0005
        shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, str(_path), None)
    except Exception:
        return


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


def _read_desktop_ini(path: Path) -> ConfigParser | None:
    parser = ConfigParser(strict=False)
    parser.optionxform = str
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            parser.read_string(raw.decode(encoding))
            return parser
        except Exception:
            continue
    return None


def _normalize_icon_spec(icon_resource: str) -> str:
    icon_spec = icon_resource.split(",", 1)[0].strip().strip('"')
    if icon_spec.casefold().endswith(".ico.0"):
        icon_spec = icon_spec[:-2]
    return icon_spec


def _resolve_icon_path(folder_path: Path, icon_spec: str) -> Path:
    icon_path = Path(icon_spec)
    if icon_path.is_absolute():
        return icon_path
    if icon_spec.startswith(".\\"):
        icon_spec = icon_spec[2:]
    return (folder_path / icon_spec).resolve()


def _is_path_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _next_available_path(folder: Path, base_name: str) -> Path:
    base = Path(base_name)
    stem = base.stem or "icon"
    suffix = base.suffix or ".ico"
    candidate = folder / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = folder / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _write_desktop_ini(parser: ConfigParser, desktop_ini: Path) -> None:
    with desktop_ini.open("w", encoding="utf-8-sig", newline="\n") as handle:
        parser.write(handle, space_around_delimiters=False)


def repair_absolute_icon_paths(roots: list[RootFolder]) -> OperationReport:
    report = OperationReport()
    for root in roots:
        root_path = Path(root.path)
        if not root_path.exists() or not root_path.is_dir():
            continue
        for child in root_path.iterdir():
            if not child.is_dir():
                continue
            desktop_ini = child / "desktop.ini"
            if not desktop_ini.exists():
                report.skipped += 1
                continue
            parser = _read_desktop_ini(desktop_ini)
            if parser is None or not parser.has_section(".ShellClassInfo"):
                report.failed += 1
                report.details.append(
                    f"{child.name}: desktop.ini is unreadable or missing [.ShellClassInfo]."
                )
                continue
            icon_resource = parser.get(".ShellClassInfo", "IconResource", fallback="").strip()
            if not icon_resource:
                report.skipped += 1
                continue

            icon_spec = _normalize_icon_spec(icon_resource)
            if not icon_spec:
                report.skipped += 1
                continue
            source_icon = _resolve_icon_path(child, icon_spec)
            if _is_path_inside(source_icon, child):
                report.skipped += 1
                continue

            report.total += 1
            if not source_icon.exists():
                report.failed += 1
                report.details.append(
                    f"{child.name}: missing external icon source '{source_icon}'."
                )
                continue

            destination_icon = _next_available_path(child, source_icon.name)
            try:
                _prepare_file_for_overwrite(source_icon)
                _prepare_file_for_overwrite(desktop_ini)
                if destination_icon.exists():
                    _prepare_file_for_overwrite(destination_icon)
                shutil.move(str(source_icon), str(destination_icon))
                parser.set(
                    ".ShellClassInfo",
                    "IconResource",
                    f".\\{destination_icon.name},0",
                )
                if not parser.has_option(".ShellClassInfo", "Flags"):
                    parser.set(".ShellClassInfo", "Flags", "0")
                _write_desktop_ini(parser, desktop_ini)
                try:
                    _run_attrib(["+s", str(child)])
                    _run_attrib(["+s", "+h", str(desktop_ini)])
                except OSError:
                    pass
                _shell_refresh(child)
                report.succeeded += 1
                report.details.append(
                    f"{child.name}: repaired icon path -> {destination_icon.name}"
                )
            except OSError as exc:
                report.failed += 1
                report.details.append(f"{child.name}: repair failed ({exc})")
    return report
