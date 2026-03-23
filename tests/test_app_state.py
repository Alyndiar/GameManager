from pathlib import Path

import pytest

from gamemanager.app_state import AppState


def test_add_root_success_and_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    root = tmp_path / "Jeux Téléchargés"
    root.mkdir()

    assert app.add_root(str(root)) == "added"
    assert app.add_root(str(root)) == "duplicate"
    assert len(app.list_roots()) == 1


def test_add_root_missing_path_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)

    with pytest.raises(ValueError, match="Folder does not exist"):
        app.add_root(str(tmp_path / "missing-folder"))


def test_sgdb_resource_preferences_persist_and_sanitize(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)

    order, enabled = app.save_sgdb_resource_preferences(
        ["heroes", "logos", "invalid", "icons"],
        {"heroes", "logos"},
    )
    assert order[:4] == ["heroes", "logos", "icons", "grids"]
    assert enabled == {"heroes", "logos"}

    loaded_order, loaded_enabled = app.sgdb_resource_preferences()
    assert loaded_order[:4] == ["heroes", "logos", "icons", "grids"]
    assert loaded_enabled == {"heroes", "logos"}
