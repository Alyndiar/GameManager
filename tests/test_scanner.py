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

