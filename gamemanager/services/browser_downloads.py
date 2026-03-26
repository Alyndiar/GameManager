from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Final


@dataclass(frozen=True, slots=True)
class BrowserDownloadDetection:
    browser_id: str
    browser_label: str
    download_dir: Path
    source: str


_CHROMIUM_FAMILY_IDS: Final[set[str]] = {
    "edge",
    "chrome",
    "chromium",
    "brave",
    "opera",
    "vivaldi",
}

_BROWSER_LABELS: Final[dict[str, str]] = {
    "edge": "Microsoft Edge",
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "firefox": "Mozilla Firefox",
}


def default_downloads_dir() -> Path:
    candidates: list[Path] = []
    home = Path.home()
    candidates.append(home / "Downloads")
    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        candidates.append(Path(user_profile) / "Downloads")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def detect_browser_download_dir() -> BrowserDownloadDetection:
    browser_id = _detect_default_browser_id()
    if browser_id == "firefox":
        hit = _detect_firefox_download_dir()
        if hit is not None:
            return hit
    if browser_id in _CHROMIUM_FAMILY_IDS:
        hit = _detect_chromium_download_dir(browser_id)
        if hit is not None:
            return hit
    for probe in ("edge", "chrome", "chromium", "brave", "opera", "vivaldi", "firefox"):
        if probe in _CHROMIUM_FAMILY_IDS:
            hit = _detect_chromium_download_dir(probe)
        else:
            hit = _detect_firefox_download_dir()
        if hit is not None:
            return hit
    fallback = default_downloads_dir()
    return BrowserDownloadDetection(
        browser_id=browser_id or "fallback",
        browser_label=_BROWSER_LABELS.get(browser_id or "", "Fallback"),
        download_dir=fallback,
        source="fallback",
    )


def _detect_default_browser_id() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except Exception:
        return ""
    user_choice = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
    prog_id = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, user_choice) as key:
            prog_id = str(winreg.QueryValueEx(key, "ProgId")[0] or "").strip()
    except OSError:
        pass
    if prog_id:
        mapped = _browser_id_from_string(prog_id)
        if mapped:
            return mapped
        command = _registry_open_command_for_progid(prog_id)
        mapped = _browser_id_from_string(command)
        if mapped:
            return mapped
    return ""


def _registry_open_command_for_progid(prog_id: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except Exception:
        return ""
    path = f"{prog_id}\\shell\\open\\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as key:
            return str(winreg.QueryValue(key, None) or "")
    except OSError:
        return ""


def _browser_id_from_string(value: str) -> str:
    raw = str(value or "").casefold()
    if not raw:
        return ""
    if "firefox" in raw:
        return "firefox"
    if "msedge" in raw or "microsoftedge" in raw:
        return "edge"
    if "brave" in raw:
        return "brave"
    if "vivaldi" in raw:
        return "vivaldi"
    if "opera" in raw:
        return "opera"
    if "chromium" in raw:
        return "chromium"
    if "chrome" in raw:
        return "chrome"
    return ""


def _detect_chromium_download_dir(browser_id: str) -> BrowserDownloadDetection | None:
    user_data_dir = _chromium_user_data_dir(browser_id)
    if user_data_dir is None or not user_data_dir.exists():
        return None
    profile_dir, source = _resolve_chromium_profile(user_data_dir)
    if profile_dir is None:
        return None
    prefs_path = profile_dir / "Preferences"
    prefs = _read_json_file(prefs_path)
    if not isinstance(prefs, dict):
        return None
    downloads = prefs.get("download")
    if not isinstance(downloads, dict):
        return None
    raw_dir = str(downloads.get("default_directory") or "").strip()
    if not raw_dir:
        return None
    resolved = Path(raw_dir).expanduser()
    if not resolved.exists():
        return None
    return BrowserDownloadDetection(
        browser_id=browser_id,
        browser_label=_BROWSER_LABELS.get(browser_id, browser_id.title()),
        download_dir=resolved,
        source=source,
    )


def _chromium_user_data_dir(browser_id: str) -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    roaming = os.environ.get("APPDATA", "").strip()
    if not local and not roaming:
        return None
    candidates: dict[str, list[Path]] = {
        "edge": [Path(local) / "Microsoft" / "Edge" / "User Data"] if local else [],
        "chrome": [Path(local) / "Google" / "Chrome" / "User Data"] if local else [],
        "chromium": [Path(local) / "Chromium" / "User Data"] if local else [],
        "brave": [Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data"] if local else [],
        "vivaldi": [Path(local) / "Vivaldi" / "User Data"] if local else [],
        "opera": [Path(roaming) / "Opera Software" / "Opera Stable"] if roaming else [],
    }
    for candidate in candidates.get(browser_id, []):
        if candidate.exists():
            return candidate
    return None


def _resolve_chromium_profile(user_data_dir: Path) -> tuple[Path | None, str]:
    if (user_data_dir / "Preferences").exists():
        return user_data_dir, "Preferences"
    local_state = _read_json_file(user_data_dir / "Local State")
    if isinstance(local_state, dict):
        profile = local_state.get("profile")
        if isinstance(profile, dict):
            last_used = str(profile.get("last_used") or "").strip()
            if last_used:
                candidate = user_data_dir / last_used
                if (candidate / "Preferences").exists():
                    return candidate, f"Local State ({last_used})"
            active = profile.get("last_active_profiles")
            if isinstance(active, list):
                for item in active:
                    label = str(item or "").strip()
                    if not label:
                        continue
                    candidate = user_data_dir / label
                    if (candidate / "Preferences").exists():
                        return candidate, f"Local State ({label})"
    default = user_data_dir / "Default"
    if (default / "Preferences").exists():
        return default, "Default profile"
    for child in sorted(user_data_dir.glob("Profile *")):
        if (child / "Preferences").exists():
            return child, f"Profile ({child.name})"
    return None, ""


def _detect_firefox_download_dir() -> BrowserDownloadDetection | None:
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return None
    root = Path(appdata) / "Mozilla" / "Firefox"
    if not root.exists():
        return None
    profile_dir, source = _resolve_firefox_profile(root)
    if profile_dir is None:
        return None
    prefs = _read_text_file(profile_dir / "prefs.js")
    if not prefs:
        return None
    folder_list = _extract_firefox_pref_int(prefs, "browser.download.folderList")
    custom_dir = _extract_firefox_pref_string(prefs, "browser.download.dir")
    if folder_list == 2 and custom_dir:
        resolved = Path(custom_dir).expanduser()
        if resolved.exists():
            return BrowserDownloadDetection(
                browser_id="firefox",
                browser_label=_BROWSER_LABELS["firefox"],
                download_dir=resolved,
                source=source,
            )
    if folder_list == 0:
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            return BrowserDownloadDetection(
                browser_id="firefox",
                browser_label=_BROWSER_LABELS["firefox"],
                download_dir=desktop,
                source=f"{source} (Desktop)",
            )
    downloads = default_downloads_dir()
    return BrowserDownloadDetection(
        browser_id="firefox",
        browser_label=_BROWSER_LABELS["firefox"],
        download_dir=downloads,
        source=f"{source} (Downloads)",
    )


def _resolve_firefox_profile(root: Path) -> tuple[Path | None, str]:
    ini_path = root / "profiles.ini"
    text = _read_text_file(ini_path)
    if text:
        sections = re.split(r"\r?\n(?=\[)", text.strip())
        default_hit: Path | None = None
        for block in sections:
            lines = [ln.strip() for ln in block.splitlines() if "=" in ln]
            payload: dict[str, str] = {}
            for line in lines:
                key, value = line.split("=", 1)
                payload[key.strip()] = value.strip()
            if "Path" not in payload:
                continue
            path_val = payload["Path"]
            is_relative = payload.get("IsRelative", "1").strip() == "1"
            candidate = (root / path_val) if is_relative else Path(path_val)
            if not candidate.exists():
                continue
            is_default = payload.get("Default", "0").strip() == "1"
            if is_default:
                return candidate, f"profiles.ini ({candidate.name})"
            if default_hit is None:
                default_hit = candidate
        if default_hit is not None:
            return default_hit, f"profiles.ini ({default_hit.name})"
    profiles_root = root / "Profiles"
    if profiles_root.exists():
        for candidate in sorted(profiles_root.iterdir()):
            if candidate.is_dir() and (candidate / "prefs.js").exists():
                return candidate, f"Profiles ({candidate.name})"
    return None, ""


def _extract_firefox_pref_string(content: str, key: str) -> str:
    pat = re.compile(
        rf'user_pref\("{re.escape(key)}"\s*,\s*"((?:\\.|[^"\\])*)"\s*\)\s*;',
        re.IGNORECASE,
    )
    match = pat.search(content)
    if not match:
        return ""
    raw = match.group(1)
    try:
        return bytes(raw, "utf-8").decode("unicode_escape")
    except Exception:
        return raw


def _extract_firefox_pref_int(content: str, key: str) -> int | None:
    pat = re.compile(
        rf'user_pref\("{re.escape(key)}"\s*,\s*([0-9]+)\s*\)\s*;',
        re.IGNORECASE,
    )
    match = pat.search(content)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _read_json_file(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

