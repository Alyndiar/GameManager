from pathlib import Path

from gamemanager.models import RootFolder
from gamemanager.services import icon_repair


def _root_folder(path: Path) -> RootFolder:
    return RootFolder(id=1, path=str(path), enabled=True, added_at="now")


def test_repair_absolute_icon_paths_moves_icon_and_rewrites_ini(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "root"
    game = root / "Game"
    ext = tmp_path / "external"
    root.mkdir()
    game.mkdir()
    ext.mkdir()
    source_icon = ext / "Shared.ico"
    source_icon.write_bytes(b"ICO")
    (game / "desktop.ini").write_text(
        f"[.ShellClassInfo]\nIconResource={source_icon},0\nFlags=0\n",
        encoding="utf-8-sig",
    )

    monkeypatch.setattr(icon_repair, "_run_attrib", lambda args: None)
    monkeypatch.setattr(icon_repair, "_shell_refresh", lambda path: None)

    report = icon_repair.repair_absolute_icon_paths([_root_folder(root)])
    assert report.succeeded == 1
    assert report.failed == 0
    moved_icon = game / "Shared.ico"
    assert moved_icon.exists()
    assert not source_icon.exists()
    ini_text = (game / "desktop.ini").read_text(encoding="utf-8-sig")
    assert "IconResource=.\\Shared.ico,0" in ini_text


def test_repair_absolute_icon_paths_skips_local_icon(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "root"
    game = root / "Game"
    root.mkdir()
    game.mkdir()
    local_icon = game / "Game.ico"
    local_icon.write_bytes(b"ICO")
    (game / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\Game.ico,0\nFlags=0\n",
        encoding="utf-8-sig",
    )

    monkeypatch.setattr(icon_repair, "_run_attrib", lambda args: None)
    monkeypatch.setattr(icon_repair, "_shell_refresh", lambda path: None)

    report = icon_repair.repair_absolute_icon_paths([_root_folder(root)])
    assert report.succeeded == 0
    assert report.failed == 0
    assert report.skipped >= 1
    assert local_icon.exists()
