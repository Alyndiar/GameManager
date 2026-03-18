from __future__ import annotations

import filecmp
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from gamemanager.app_state import AppState
from gamemanager.ui.main_window import MainWindow


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


def _project_data_dir() -> Path:
    override = os.environ.get("GAMEMANAGER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Keep all non-registry state inside the project workspace by default.
    project_root = Path(__file__).resolve().parent.parent
    return project_root / ".gamemanager_data"


def _default_db_path() -> Path:
    data_dir = _project_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data(data_dir)
    return data_dir / "manager.db"


def run() -> int:
    app = QApplication(sys.argv)
    state = AppState(_default_db_path())
    window = MainWindow(state)
    screen = app.primaryScreen()
    if screen is not None:
        window.setGeometry(screen.availableGeometry())
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
