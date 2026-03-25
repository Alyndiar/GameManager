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


def test_get_or_fetch_game_infotip_caches_result(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    calls = {"count": 0}

    def _fake_fetch(name: str):
        calls["count"] += 1
        return ("One line description.", "steam")

    monkeypatch.setattr("gamemanager.app_state.fetch_game_infotip", _fake_fetch)
    first = app.get_or_fetch_game_infotip("Test Game")
    second = app.get_or_fetch_game_infotip("test game")
    assert first == "One line description."
    assert second == "One line description."
    assert calls["count"] == 1


def test_set_manual_folder_info_tip_updates_cache(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    folder = tmp_path / "Game"
    folder.mkdir()
    calls: list[tuple[str, str]] = []

    def _fake_read(_path):
        return ""

    def _fake_set(path, tip):
        calls.append((str(path), tip))
        return True

    monkeypatch.setattr("gamemanager.app_state.read_folder_info_tip", _fake_read)
    monkeypatch.setattr("gamemanager.app_state.set_folder_info_tip", _fake_set)

    ok = app.set_manual_folder_info_tip(
        str(folder),
        "Some Game",
        "Manual line.",
    )
    assert ok is True
    assert calls and calls[0][1] == "Manual line."
    cached = app.db.get_game_infotip("some game")
    assert cached is not None
    tip, source = cached
    assert tip == "Manual line."
    assert source == "manual"
