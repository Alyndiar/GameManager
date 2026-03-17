from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from gamemanager.app_state import AppState
from gamemanager.ui.main_window import MainWindow


def _default_db_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "GameBackupManager" / "manager.db"
    return Path.home() / ".game_backup_manager" / "manager.db"


def run() -> int:
    app = QApplication(sys.argv)
    state = AppState(_default_db_path())
    window = MainWindow(state)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())

