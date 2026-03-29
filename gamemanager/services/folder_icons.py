from __future__ import annotations

import ctypes
import os
import re
import subprocess
from configparser import ConfigParser
from pathlib import Path

from gamemanager.models import IconApplyResult


_INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*]+')
_SHELLCLASSINFO_SECTION = ".ShellClassInfo"
_GM_ICON_SECTION = "GameManager.Icon"
_GM_ICON_KEYS = (
    "SourceKind",
    "SourceProvider",
    "SourceCandidateId",
    "SourceGameId",
    "SourceUrl",
    "SourceFingerprint256",
    "SourceConfidence",
    "SourceAssignedAtUtc",
    "SourceBackfillAtUtc",
    "SourceBackfillFingerprint256",
)


def _sanitize_icon_name(raw_name: str) -> str:
    normalized = _INVALID_FILE_CHARS.sub(" ", raw_name)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        normalized = "folder"
    return normalized


def _read_desktop_ini_parser(desktop_ini: Path) -> ConfigParser | None:
    try:
        raw = desktop_ini.read_bytes()
    except OSError:
        return None
    decoded: str | None = None
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            decoded = raw.decode(encoding)
            break
        except Exception:
            continue
    if decoded is None:
        return None

    parser = ConfigParser(strict=False)
    parser.optionxform = str
    try:
        parser.read_string(decoded)
        return parser
    except Exception:
        pass

    # Tolerate malformed desktop.ini values (for example multiline InfoTip text
    # that is not continuation-indented) by keeping only section and key=value lines.
    sanitized_lines: list[str] = []
    for line in decoded.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            sanitized_lines.append(stripped)
            continue
        if "=" in line:
            sanitized_lines.append(line)
    if not sanitized_lines:
        return None
    parser = ConfigParser(strict=False)
    parser.optionxform = str
    try:
        parser.read_string("\n".join(sanitized_lines))
        return parser
    except Exception:
        return None


def _sanitize_info_tip_value(raw_tip: str | None) -> str:
    token = str(raw_tip or "")
    # desktop.ini expects single-line INI values.
    token = token.replace("\r", " ").replace("\n", " ")
    token = re.sub(r"\s+", " ", token).strip()
    return token


def _read_existing_info_tip(parser: ConfigParser) -> str:
    return _sanitize_info_tip_value(
        parser.get(_SHELLCLASSINFO_SECTION, "InfoTip", fallback="")
    )


def _read_existing_flags(parser: ConfigParser) -> str:
    flags = parser.get(_SHELLCLASSINFO_SECTION, "Flags", fallback="0").strip() or "0"
    return flags


def _read_existing_rebuilt(parser: ConfigParser) -> bool:
    token = str(
        parser.get(_SHELLCLASSINFO_SECTION, "Rebuilt", fallback="false")
    ).strip().casefold()
    return token in {"1", "true", "yes", "on"}


def _normalize_icon_resource(icon_resource: str) -> str:
    return str(icon_resource or "").strip()


def _sanitize_metadata_value(raw_value: str | None) -> str:
    token = str(raw_value or "")
    token = token.replace("\r", " ").replace("\n", " ")
    token = re.sub(r"\s+", " ", token).strip()
    return token


def _read_existing_icon_metadata(parser: ConfigParser) -> dict[str, str]:
    if not parser.has_section(_GM_ICON_SECTION):
        return {}
    out: dict[str, str] = {}
    for key in _GM_ICON_KEYS:
        value = _sanitize_metadata_value(parser.get(_GM_ICON_SECTION, key, fallback=""))
        if value:
            out[key] = value
    return out


def _set_icon_metadata(
    parser: ConfigParser,
    metadata: dict[str, str] | None,
) -> None:
    payload = dict(metadata or {})
    clean: dict[str, str] = {}
    for key in _GM_ICON_KEYS:
        if key not in payload:
            continue
        value = _sanitize_metadata_value(payload.get(key))
        if value:
            clean[key] = value
    if not clean:
        if parser.has_section(_GM_ICON_SECTION):
            parser.remove_section(_GM_ICON_SECTION)
        return
    if not parser.has_section(_GM_ICON_SECTION):
        parser.add_section(_GM_ICON_SECTION)
    for key in list(parser.options(_GM_ICON_SECTION)):
        if key not in clean:
            parser.remove_option(_GM_ICON_SECTION, key)
    for key, value in clean.items():
        parser.set(_GM_ICON_SECTION, key, value)


def _write_desktop_ini(
    desktop_ini: Path,
    folder_path: Path,
    parser: ConfigParser,
) -> None:
    _prepare_file_for_overwrite(desktop_ini)
    with desktop_ini.open("w", encoding="utf-8-sig", newline="\n") as handle:
        parser.write(handle, space_around_delimiters=False)
    _run_attrib(["+s", "+h", str(desktop_ini)])
    _run_attrib(["+s", str(folder_path)])
    _shell_refresh(folder_path)


def _write_shellclassinfo(
    desktop_ini: Path,
    folder_path: Path,
    *,
    icon_resource: str,
    info_tip: str | None = None,
    flags: str = "0",
    rebuilt: bool = False,
    parser: ConfigParser | None = None,
) -> None:
    current = parser
    if current is None:
        current = ConfigParser(strict=False)
        current.optionxform = str
        current.optionxform = str
    if not current.has_section(_SHELLCLASSINFO_SECTION):
        current.add_section(_SHELLCLASSINFO_SECTION)
    current.set(_SHELLCLASSINFO_SECTION, "IconResource", _normalize_icon_resource(icon_resource))
    tip = _sanitize_info_tip_value(info_tip)
    if tip:
        current.set(_SHELLCLASSINFO_SECTION, "InfoTip", tip)
    elif current.has_option(_SHELLCLASSINFO_SECTION, "InfoTip"):
        current.remove_option(_SHELLCLASSINFO_SECTION, "InfoTip")
    current.set(_SHELLCLASSINFO_SECTION, "Flags", flags or "0")
    current.set(_SHELLCLASSINFO_SECTION, "Rebuilt", "true" if rebuilt else "false")
    _write_desktop_ini(desktop_ini, folder_path, current)


def _resolve_icon_path(folder_path: Path, icon_spec: str) -> Path:
    spec = str(icon_spec or "").strip().strip('"')
    if spec.casefold().endswith(".ico.0"):
        spec = spec[:-2]
    if spec.startswith(".\\"):
        spec = spec[2:]
    path_token = Path(spec)
    if path_token.is_absolute():
        return path_token.resolve()
    return (folder_path / spec).resolve()


def _extract_icon_resource(parser: ConfigParser) -> str:
    return str(parser.get(_SHELLCLASSINFO_SECTION, "IconResource", fallback="")).strip()


def _icon_spec_from_resource(icon_resource: str) -> str:
    return str(icon_resource or "").split(",", 1)[0].strip().strip('"')


def detect_folder_icon_state(folder_path: Path) -> tuple[str, str | None, str | None, str]:
    if not folder_path.exists() or not folder_path.is_dir():
        return "none", None, None, ""

    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return "none", None, None, ""
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None:
        return "broken", None, str(desktop_ini), ""
    if not parser.has_section(_SHELLCLASSINFO_SECTION):
        return "broken", None, str(desktop_ini), ""
    icon_resource = _extract_icon_resource(parser)
    info_tip = _read_existing_info_tip(parser)
    if not icon_resource:
        return "broken", None, str(desktop_ini), info_tip
    icon_spec = _icon_spec_from_resource(icon_resource)
    icon_path = _resolve_icon_path(folder_path, icon_spec)
    if not icon_path.exists():
        return "broken", str(icon_path), str(desktop_ini), info_tip
    return "valid", str(icon_path), str(desktop_ini), info_tip


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


def _prepare_file_for_overwrite(path: Path) -> None:
    if not path.exists():
        return
    try:
        _run_attrib(["-r", "-s", "-h", str(path)])
    except OSError:
        # Continue and attempt write; attrib can fail on some FS setups.
        pass
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def read_folder_info_tip(folder_path: Path) -> str:
    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return ""
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None or not parser.has_section(_SHELLCLASSINFO_SECTION):
        return ""
    return _read_existing_info_tip(parser)


def set_folder_info_tip(folder_path: Path, info_tip: str) -> bool:
    cleaned_tip = _sanitize_info_tip_value(info_tip)
    if not cleaned_tip:
        return False
    if not folder_path.exists() or not folder_path.is_dir():
        return False
    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return False
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None or not parser.has_section(_SHELLCLASSINFO_SECTION):
        return False
    icon_resource = _extract_icon_resource(parser)
    if not icon_resource:
        return False
    current_tip = _read_existing_info_tip(parser)
    if current_tip == cleaned_tip:
        return False
    flags = _read_existing_flags(parser)
    rebuilt = _read_existing_rebuilt(parser)
    try:
        _write_shellclassinfo(
            desktop_ini,
            folder_path,
            icon_resource=icon_resource,
            info_tip=cleaned_tip,
            flags=flags,
            rebuilt=rebuilt,
            parser=parser,
        )
    except OSError:
        return False
    return True


def read_folder_rebuilt_flag(folder_path: Path) -> bool:
    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return False
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None or not parser.has_section(_SHELLCLASSINFO_SECTION):
        return False
    return _read_existing_rebuilt(parser)


def set_folder_rebuilt_flag(folder_path: Path, rebuilt: bool) -> bool:
    if not folder_path.exists() or not folder_path.is_dir():
        return False
    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return False
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None or not parser.has_section(_SHELLCLASSINFO_SECTION):
        return False
    icon_resource = _extract_icon_resource(parser)
    if not icon_resource:
        return False
    current_rebuilt = _read_existing_rebuilt(parser)
    if current_rebuilt == bool(rebuilt):
        return False
    info_tip = _read_existing_info_tip(parser)
    flags = _read_existing_flags(parser)
    try:
        _write_shellclassinfo(
            desktop_ini,
            folder_path,
            icon_resource=icon_resource,
            info_tip=info_tip,
            flags=flags,
            rebuilt=bool(rebuilt),
            parser=parser,
        )
    except OSError:
        return False
    return True


def read_folder_icon_metadata(folder_path: Path) -> dict[str, str]:
    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return {}
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None or not parser.has_section(_SHELLCLASSINFO_SECTION):
        return {}
    return _read_existing_icon_metadata(parser)


def set_folder_icon_metadata(
    folder_path: Path,
    metadata: dict[str, str] | None,
) -> bool:
    if not folder_path.exists() or not folder_path.is_dir():
        return False
    desktop_ini = folder_path / "desktop.ini"
    if not desktop_ini.exists():
        return False
    parser = _read_desktop_ini_parser(desktop_ini)
    if parser is None or not parser.has_section(_SHELLCLASSINFO_SECTION):
        return False
    current = _read_existing_icon_metadata(parser)
    normalized: dict[str, str] = {}
    for key in _GM_ICON_KEYS:
        if not isinstance(metadata, dict) or key not in metadata:
            continue
        value = _sanitize_metadata_value(metadata.get(key))
        if value:
            normalized[key] = value
    if current == normalized:
        return False
    _set_icon_metadata(parser, normalized)
    icon_resource = _extract_icon_resource(parser)
    if not icon_resource:
        return False
    info_tip = _read_existing_info_tip(parser)
    flags = _read_existing_flags(parser)
    rebuilt = _read_existing_rebuilt(parser)
    try:
        _write_shellclassinfo(
            desktop_ini,
            folder_path,
            icon_resource=icon_resource,
            info_tip=info_tip,
            flags=flags,
            rebuilt=rebuilt,
            parser=parser,
        )
    except OSError:
        return False
    return True


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
    existing_parser: ConfigParser | None = None
    if desktop_ini.exists():
        parsed = _read_desktop_ini_parser(desktop_ini)
        if parsed is not None and parsed.has_section(_SHELLCLASSINFO_SECTION):
            existing_parser = parsed
    try:
        _prepare_file_for_overwrite(icon_path)
        icon_path.write_bytes(icon_bytes)
        _write_shellclassinfo(
            desktop_ini,
            folder_path,
            icon_resource=f".\\{icon_path.name},0",
            info_tip=info_tip,
            flags="0",
            rebuilt=True,
            parser=existing_parser,
        )
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
