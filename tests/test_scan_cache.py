from pathlib import Path

import pytest

from gamemanager.models import RootFolder
from gamemanager.services.scan_cache import DirectorySizeCache
from gamemanager.services import scanner as scanner_mod


def test_directory_size_cache_roundtrip(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "dir_sizes.json"
    cache = DirectorySizeCache(cache_path, max_entries=10_000)
    target = tmp_path / "folder"
    target.mkdir()
    mtime_ns = target.stat().st_mtime_ns

    assert cache.get(target, mtime_ns) is None
    cache.put(target, mtime_ns, 12345)
    cache.save()

    loaded = DirectorySizeCache(cache_path, max_entries=10_000)
    assert loaded.get(target, mtime_ns) == 12345


def test_scan_roots_reuses_cached_directory_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    game_dir = root / "GameFolder"
    game_dir.mkdir()
    (game_dir / "file.bin").write_bytes(b"x" * 20)
    roots = [RootFolder(id=1, path=str(root), enabled=True, added_at="now")]

    cache = DirectorySizeCache(tmp_path / "cache" / "sizes.json")
    first = scanner_mod.scan_roots(roots, approved_tags=set(), dir_size_cache=cache)
    folder_item = next(item for item in first if item.full_name == "GameFolder")
    assert folder_item.size_bytes == 20

    def _fail_size(_path):
        raise AssertionError("directory walk should not run when cache is valid")

    monkeypatch.setattr(scanner_mod, "_directory_size_bytes", _fail_size)
    second = scanner_mod.scan_roots(roots, approved_tags=set(), dir_size_cache=cache)
    second_item = next(item for item in second if item.full_name == "GameFolder")
    assert second_item.size_bytes == 20

