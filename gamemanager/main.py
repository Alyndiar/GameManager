from __future__ import annotations

from datetime import datetime
import faulthandler
import filecmp
import os
import shutil
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from gamemanager.runtime import (
    DEFAULT_ICONMAKER_GAMEMANAGER_MUTEX,
    AppInstanceLock,
    show_already_running_message,
)
from gamemanager.services.paths import project_data_dir
from gamemanager.services.persistent_workers import (
    ensure_persistent_icon_workers_async,
    shutdown_persistent_icon_workers,
)
_CRASH_LOG_HANDLE = None


def _configure_runtime_noise_controls() -> None:
    # Hide low-value PNG metadata duplicate warnings (eXIf duplicate).
    existing_rules = os.environ.get("QT_LOGGING_RULES", "").strip()
    rule = "qt.gui.imageio.warning=false"
    if not existing_rules:
        os.environ["QT_LOGGING_RULES"] = rule
    elif rule not in existing_rules:
        os.environ["QT_LOGGING_RULES"] = f"{existing_rules};{rule}"
    # Keep icon text/cutout workflows fully local and skip Paddle model host checks.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def _legacy_appdata_data_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "GameBackupManager"
    return None


def _remove_if_empty(path: Path) -> None:
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        return


def _merge_move_cache_dir(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        src_path = src / entry.name
        dst_path = dst / entry.name
        if src_path.is_dir():
            _merge_move_cache_dir(src_path, dst_path)
            _remove_if_empty(src_path)
            continue
        if not dst_path.exists():
            shutil.move(str(src_path), str(dst_path))
            continue
        try:
            same = filecmp.cmp(src_path, dst_path, shallow=False)
        except OSError:
            same = False
        if same:
            try:
                src_path.unlink()
            except OSError:
                pass


def _migrate_legacy_data(target_dir: Path) -> None:
    legacy_dir = _legacy_appdata_data_dir()
    if legacy_dir is None or not legacy_dir.exists():
        return
    target_db = target_dir / "manager.db"
    legacy_db = legacy_dir / "manager.db"
    legacy_cache = legacy_dir / "cache"
    target_cache = target_dir / "cache"

    if legacy_db.exists():
        if not target_db.exists():
            try:
                shutil.move(str(legacy_db), str(target_db))
            except OSError:
                pass
        else:
            try:
                same = filecmp.cmp(legacy_db, target_db, shallow=False)
            except OSError:
                same = False
            if same:
                try:
                    legacy_db.unlink()
                except OSError:
                    pass

    if legacy_cache.exists() and legacy_cache.is_dir():
        try:
            if not target_cache.exists():
                shutil.move(str(legacy_cache), str(target_cache))
            else:
                _merge_move_cache_dir(legacy_cache, target_cache)
        except OSError:
            pass
        _remove_if_empty(legacy_cache)

    _remove_if_empty(legacy_dir)


def _default_db_path() -> Path:
    data_dir = project_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data(data_dir)
    return data_dir / "manager.db"


def _enable_crash_logging(data_dir: Path) -> None:
    global _CRASH_LOG_HANDLE
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "crash.log"
    try:
        _CRASH_LOG_HANDLE = open(log_path, "a", encoding="utf-8")
    except OSError:
        _CRASH_LOG_HANDLE = None
        return

    _CRASH_LOG_HANDLE.write(
        f"\n[{datetime.now().isoformat(timespec='seconds')}] GameManager session start\n"
    )
    _CRASH_LOG_HANDLE.flush()
    try:
        faulthandler.enable(_CRASH_LOG_HANDLE, all_threads=True)
    except Exception:
        pass

    def _log_excepthook(exc_type, exc_value, exc_tb):
        if _CRASH_LOG_HANDLE is not None:
            _CRASH_LOG_HANDLE.write(
                f"[{datetime.now().isoformat(timespec='seconds')}] Unhandled exception:\n"
            )
            traceback.print_exception(exc_type, exc_value, exc_tb, file=_CRASH_LOG_HANDLE)
            _CRASH_LOG_HANDLE.flush()
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_excepthook


def run() -> int:
    instance_lock = AppInstanceLock(DEFAULT_ICONMAKER_GAMEMANAGER_MUTEX)
    if not instance_lock.acquire():
        show_already_running_message(
            current_app_name="GameManager",
            other_app_name="GameManager or IconMaker",
        )
        return 1
    state = None
    window = None
    _configure_runtime_noise_controls()
    db_path = _default_db_path()
    _enable_crash_logging(db_path.parent)
    app = QApplication(sys.argv)
    app.aboutToQuit.connect(shutdown_persistent_icon_workers)
    try:
        from gamemanager.app_state import AppState
        from gamemanager.ui.main_window import MainWindow
    except Exception:
        instance_lock.release()
        raise
    state = AppState(db_path)
    window = MainWindow(state)
    screen = app.primaryScreen()
    if screen is not None:
        window.setGeometry(screen.availableGeometry())
    window.showMaximized()
    QTimer.singleShot(1200, lambda: ensure_persistent_icon_workers_async(worker_count=2))
    try:
        return app.exec()
    finally:
        shutdown_persistent_icon_workers()
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(run())
