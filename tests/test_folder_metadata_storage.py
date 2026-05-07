from pathlib import Path

from gamemanager.app_state import AppState
from gamemanager.services import folder_icons


def test_legacy_desktop_ini_metadata_is_migrated_to_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(folder_icons, "_run_attrib", lambda _args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda _path: None)

    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "game"
    folder.mkdir(parents=True)
    (folder / "Game.ico").write_bytes(b"\x00")
    (folder / "desktop.ini").write_text(
        (
            "[.ShellClassInfo]\n"
            "IconResource=.\\Game.ico,0\n"
            "InfoTip=Tip\n"
            "Flags=0\n"
            "Rebuilt=true\n\n"
            "[GameManager.Icon]\n"
            "SourceKind=sgdb_raw\n"
            "SourceProvider=SteamGridDB\n"
        ),
        encoding="utf-8-sig",
    )

    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "sgdb_raw"
    assert metadata.get("SourceProvider") == "SteamGridDB"

    desktop = (folder / "desktop.ini").read_text(encoding="utf-8-sig")
    assert "[GameManager.Icon]" not in desktop
    assert "InfoTip=Tip" in desktop
    assert "Rebuilt=true" in desktop


def test_upsert_folder_metadata_writes_db_not_desktop_ini(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(folder_icons, "_run_attrib", lambda _args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda _path: None)

    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "game"
    folder.mkdir(parents=True)
    (folder / "Game.ico").write_bytes(b"\x00")
    (folder / "desktop.ini").write_text(
        (
            "[.ShellClassInfo]\n"
            "IconResource=.\\Game.ico,0\n"
            "InfoTip=Tip\n"
            "Flags=0\n"
            "Rebuilt=true\n"
        ),
        encoding="utf-8-sig",
    )

    changed = app.upsert_folder_icon_metadata(
        str(folder),
        {"SourceKind": "web", "SourceProvider": "Internet"},
    )
    assert changed is True

    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "web"
    assert metadata.get("SourceProvider") == "Internet"

    desktop = (folder / "desktop.ini").read_text(encoding="utf-8-sig")
    assert "SourceKind=" not in desktop
    assert "SourceProvider=" not in desktop
