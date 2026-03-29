from __future__ import annotations

from pathlib import Path


def gamemanager_app_icon_path() -> Path:
    """Return the canonical GameManager application icon path."""
    return Path(__file__).resolve().parents[1] / "assets" / "GameManager.ico"


def apply_gamemanager_app_icon(app) -> bool:
    """Set QApplication-level icon for GameManager.

    Returns True when an icon was applied.
    """
    icon_path = gamemanager_app_icon_path()
    if not icon_path.exists():
        return False
    try:
        from PySide6.QtGui import QIcon
    except Exception:
        return False
    icon = QIcon(str(icon_path))
    if icon.isNull():
        return False
    app.setWindowIcon(icon)
    return True
