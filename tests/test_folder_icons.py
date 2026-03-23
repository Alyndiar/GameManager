from pathlib import Path

from gamemanager.services import folder_icons


def test_detect_folder_icon_state_valid(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    icon_path.write_bytes(b"ICO")
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\Game.ico,0\nFlags=0\n",
        encoding="utf-8-sig",
    )

    status, icon_file, desktop_ini = folder_icons.detect_folder_icon_state(folder)
    assert status == "valid"
    assert icon_file is not None and icon_file.endswith("Game.ico")
    assert desktop_ini is not None and desktop_ini.endswith("desktop.ini")


def test_detect_folder_icon_state_broken_when_missing_target(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\Missing.ico,0\nFlags=0\n",
        encoding="utf-8-sig",
    )
    status, icon_file, _ = folder_icons.detect_folder_icon_state(folder)
    assert status == "broken"
    assert icon_file is not None and icon_file.endswith("Missing.ico")


def test_detect_folder_icon_state_accepts_dot_zero_suffix(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / "Game.ico").write_bytes(b"ICO")
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\Game.ico.0\nFlags=0\n",
        encoding="utf-8-sig",
    )
    status, icon_file, _ = folder_icons.detect_folder_icon_state(folder)
    assert status == "valid"
    assert icon_file is not None and icon_file.endswith("Game.ico")


def test_apply_folder_icon_writes_ini_and_icon(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)

    result = folder_icons.apply_folder_icon(
        folder_path=folder,
        icon_bytes=b"ICONDATA",
        icon_name_hint="Test Game",
        info_tip="My tip",
    )
    assert result.status == "applied"
    assert (folder / "Test Game.ico").exists()
    desktop = (folder / "desktop.ini").read_text(encoding="utf-8-sig")
    assert "[.ShellClassInfo]" in desktop
    assert "IconResource=.\\Test Game.ico,0" in desktop
    assert "InfoTip=My tip" in desktop


def test_apply_folder_icon_clears_attrs_before_overwrite(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    existing_icon = folder / "Game.ico"
    existing_ini = folder / "desktop.ini"
    existing_icon.write_bytes(b"OLD")
    existing_ini.write_text(
        "[.ShellClassInfo]\nIconResource=.\\Game.ico,0\nFlags=0\n",
        encoding="utf-8-sig",
    )

    attrib_calls: list[list[str]] = []

    def _capture_attrib(args: list[str]) -> None:
        attrib_calls.append(args)

    monkeypatch.setattr(folder_icons, "_run_attrib", _capture_attrib)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)

    result = folder_icons.apply_folder_icon(
        folder_path=folder,
        icon_bytes=b"NEW",
        icon_name_hint="Game",
    )
    assert result.status == "applied"
    clear_icon = ["-r", "-s", "-h", str(existing_icon)]
    clear_ini = ["-r", "-s", "-h", str(existing_ini)]
    assert clear_icon in attrib_calls
    assert clear_ini in attrib_calls
