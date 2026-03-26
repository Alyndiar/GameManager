from __future__ import annotations

import ctypes
import os
from pathlib import Path

from gamemanager.services.paths import project_data_dir


DEFAULT_ICONMAKER_GAMEMANAGER_MUTEX = r"Global\GameManager.IconMaker.MutualExclusive.v1"
_ERROR_ALREADY_EXISTS = 183


class AppInstanceLock:
    """Best-effort single-instance lock with Windows named mutex support."""

    def __init__(self, name: str):
        self._name = str(name).strip() or DEFAULT_ICONMAKER_GAMEMANAGER_MUTEX
        self._handle: int | None = None
        self._file_handle = None
        self._lock_path: Path | None = None

    def acquire(self) -> bool:
        if self._handle is not None or self._file_handle is not None:
            return True
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateMutexW(None, False, self._name)
            if not handle:
                return False
            error = ctypes.get_last_error()
            if error == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = int(handle)
            return True
        lock_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in self._name)
        lock_dir = project_data_dir() / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{lock_name}.lock"
        try:
            self._file_handle = open(lock_path, "x", encoding="utf-8")
            self._file_handle.write(str(os.getpid()))
            self._file_handle.flush()
            self._lock_path = lock_path
            return True
        except FileExistsError:
            return False
        except OSError:
            return False

    def release(self) -> None:
        if os.name == "nt":
            if self._handle is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(self._handle)
                self._handle = None
            return
        handle = self._file_handle
        self._file_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if self._lock_path is not None:
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._lock_path = None


def show_already_running_message(
    *,
    current_app_name: str,
    other_app_name: str,
) -> None:
    message = f"{current_app_name} cannot start because {other_app_name} is already running."
    print(message)
    # Optional interactive warning. Disabled by default to avoid hidden modal stalls.
    if os.environ.get("GAMEMANAGER_BLOCKING_MUTEX_MESSAGE", "").strip() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    if os.name != "nt":
        return
    text = (
        f"{other_app_name} is already running.\n\n"
        f"{current_app_name} cannot start while {other_app_name} is active."
    )
    try:
        ctypes.windll.user32.MessageBoxW(None, text, current_app_name, 0x00000030)
    except Exception:
        return
