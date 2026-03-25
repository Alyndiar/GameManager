from pathlib import Path

from gamemanager.models import RootFolder
from gamemanager.services.scanner import scan_roots


def test_scan_roots_computes_recursive_directory_size(tmp_path: Path) -> None:
    root_dir = tmp_path / "root"
    root_dir.mkdir()

    folder = root_dir / "GameFolder"
    folder.mkdir()
    (folder / "a.bin").write_bytes(b"1" * 10)
    nested = folder / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"2" * 25)
    (root_dir / "single.iso").write_bytes(b"3" * 7)

    roots = [RootFolder(id=1, path=str(root_dir), enabled=True, added_at="now")]
    items = scan_roots(roots, approved_tags=set())
    by_name = {item.full_name: item for item in items}

    assert by_name["GameFolder"].is_dir
    assert by_name["GameFolder"].size_bytes == 35
    assert by_name["single.iso"].size_bytes == 7


def test_scan_roots_detects_valid_folder_icon_metadata(tmp_path: Path) -> None:
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    folder = root_dir / "GameFolder"
    folder.mkdir()
    (folder / "GameFolder.ico").write_bytes(b"ICO")
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\GameFolder.ico,0\nFlags=0\n",
        encoding="utf-8-sig",
    )

    roots = [RootFolder(id=1, path=str(root_dir), enabled=True, added_at="now")]
    items = scan_roots(roots, approved_tags=set())
    item = next(x for x in items if x.full_name == "GameFolder")
    assert item.icon_status == "valid"
    assert item.folder_icon_path is not None and item.folder_icon_path.endswith("GameFolder.ico")
    assert item.desktop_ini_path is not None and item.desktop_ini_path.endswith("desktop.ini")
    assert item.info_tip == ""

