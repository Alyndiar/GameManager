from pathlib import Path

from gamemanager.models import RootFolder
from gamemanager.services.storage import (
    get_root_display_info,
    mountpoint_sort_key,
    source_label_for_path,
)


def test_source_label_prefers_mount_folder_name_under_e_mount() -> None:
    root = Path(r"E:\Mount\GamesSSD\Backups")
    mountpoint = r"E:\Mount\GamesSSD"
    assert source_label_for_path(root, mountpoint) == "GamesSSD"


def test_source_label_uses_mount_name_when_mountpoint_is_under_e_mount() -> None:
    root = Path(r"D:\Games")
    mountpoint = r"E:\Mount\Archive01"
    assert source_label_for_path(root, mountpoint) == "Archive01"


def test_mountpoint_sort_key_is_normalized_case_insensitive() -> None:
    assert mountpoint_sort_key(r"E:\Mount\GamesSSD\\") == mountpoint_sort_key(
        r"e:\mount\gamesssd"
    )


def test_drive_name_uses_mount_name_even_if_resolve_returns_drive_letter(
    monkeypatch,
) -> None:
    def fake_resolve_mountpoint(path: Path) -> str:
        return r"E:\\"

    def fake_volume_label(volume_root: str) -> str:
        return "WrongVolumeLabel"

    monkeypatch.setattr(
        "gamemanager.services.storage._resolve_mountpoint", fake_resolve_mountpoint
    )
    monkeypatch.setattr("gamemanager.services.storage._volume_label", fake_volume_label)

    root = RootFolder(
        id=1,
        path=r"E:\Mount\GamesSSD\Backups",
        enabled=True,
        added_at="now",
    )
    info = get_root_display_info(root)
    assert info.drive_name == "GamesSSD"


def test_free_space_prefers_root_path_usage_for_mounted_volume(monkeypatch) -> None:
    class _Usage:
        def __init__(self, total: int, free: int) -> None:
            self.total = total
            self.free = free

    def fake_resolve_mountpoint(path: Path) -> str:
        return r"E:\\"

    def fake_disk_usage(path_value):
        path_str = str(path_value).casefold()
        if "e:\\mount\\gamesssd\\backups" in path_str:
            return _Usage(999999999, 123456789)
        if path_str in {r"e:\\", r"e:"}:
            return _Usage(222, 111)
        raise FileNotFoundError(path_str)

    monkeypatch.setattr(
        "gamemanager.services.storage._resolve_mountpoint", fake_resolve_mountpoint
    )
    monkeypatch.setattr("gamemanager.services.storage.shutil.disk_usage", fake_disk_usage)
    monkeypatch.setattr("gamemanager.services.storage._volume_label", lambda _: "VOL")

    root = RootFolder(
        id=1,
        path=r"E:\Mount\GamesSSD\Backups",
        enabled=True,
        added_at="now",
    )
    info = get_root_display_info(root)
    assert info.total_size_bytes == 999999999
    assert info.free_space_bytes == 123456789
