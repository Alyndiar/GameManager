from pathlib import Path
from io import BytesIO

from PIL import Image
import pytest

from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.icon_readability import (
    build_rebuild_preview_frames,
    clean_backup_icon_files,
    collect_existing_local_icons,
    rebuild_existing_local_icons,
)


def _single_size_ico(path: Path, size: int = 32) -> None:
    image = Image.new("RGBA", (size, size), (60, 90, 180, 255))
    image.save(path, format="ICO", sizes=[(size, size)])


def _desktop_ini(folder: Path, icon_name: str = "Game.ico", rebuilt: str = "false") -> Path:
    desktop_ini = folder / "desktop.ini"
    desktop_ini.write_text(
        "[.ShellClassInfo]\n"
        f"IconResource=.\\{icon_name},0\n"
        "Flags=0\n"
        f"Rebuilt={rebuilt}\n",
        encoding="utf-8-sig",
    )
    return desktop_ini


def test_collect_existing_local_icons_reads_rebuilt_status(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=32)
    _desktop_ini(folder, rebuilt="true")

    report, entries = collect_existing_local_icons([(folder, icon_path)])

    assert report.total == 1
    assert len(entries) == 1
    assert entries[0].already_rebuilt is True
    assert "Already rebuilt" in entries[0].summary


def test_rebuild_existing_local_icons_creates_backup_and_sets_rebuilt_true(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=32)
    desktop_ini = _desktop_ini(folder, rebuilt="false")

    _report, entries = collect_existing_local_icons([(folder, icon_path)])
    rebuild_report = rebuild_existing_local_icons(entries)
    assert rebuild_report.succeeded == 1
    assert icon_path.exists()
    backup_files = list(folder.glob("Game.gm_backup_*.ico"))
    assert backup_files

    rebuilt = Image.open(icon_path)
    sizes = rebuilt.info.get("sizes") or set()
    assert (16, 16) in sizes and (24, 24) in sizes and (32, 32) in sizes and (48, 48) in sizes

    desktop = desktop_ini.read_text(encoding="utf-8-sig")
    assert "IconResource=.\\Game.ico,0" in desktop
    assert "Rebuilt=true" in desktop


def test_rebuild_existing_local_icons_skips_rebuilt_entries_by_default(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=48)
    _desktop_ini(folder, rebuilt="true")

    _report, entries = collect_existing_local_icons([(folder, icon_path)])
    report = rebuild_existing_local_icons(entries)
    assert report.skipped == 1
    assert report.succeeded == 0


def test_rebuild_existing_local_icons_force_rebuild_allows_rebuilt_entries(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=48)
    _desktop_ini(folder, rebuilt="true")

    _report, entries = collect_existing_local_icons([(folder, icon_path)])
    report = rebuild_existing_local_icons(entries, force_rebuild=True)
    assert report.succeeded == 1
    assert report.skipped == 0


def test_rebuild_existing_local_icons_can_disable_backups(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=32)
    _desktop_ini(folder, rebuilt="false")
    _report, entries = collect_existing_local_icons([(folder, icon_path)])
    rebuild_report = rebuild_existing_local_icons(entries, create_backups=False)
    assert rebuild_report.succeeded == 1
    assert list(folder.glob("Game.gm_backup_*.ico")) == []


def test_clean_backup_icon_files_deletes_only_backup_icons(tmp_path: Path) -> None:
    root = tmp_path / "Root"
    game = root / "Game"
    game.mkdir(parents=True)
    backup_one = game / "Game.gm_backup_20260101010101.ico"
    backup_two = game / "Game.gm_backup_20260101010102.ico"
    regular = game / "Game.ico"
    _single_size_ico(backup_one, size=32)
    _single_size_ico(backup_two, size=32)
    _single_size_ico(regular, size=32)

    report = clean_backup_icon_files([root])

    assert report.total == 2
    assert report.succeeded == 2
    assert report.failed == 0
    assert not backup_one.exists()
    assert not backup_two.exists()
    assert regular.exists()


def test_build_rebuild_preview_frames_returns_png_pairs(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=32)
    _desktop_ini(folder, rebuilt="false")
    _report, entries = collect_existing_local_icons([(folder, icon_path)])
    entry = entries[0]

    frames = build_rebuild_preview_frames(entry)

    assert set(frames.keys()) == {16, 24, 32, 48}
    for size, (before_png, after_png) in frames.items():
        before = Image.open(BytesIO(before_png))
        after = Image.open(BytesIO(after_png))
        assert before.format == "PNG"
        assert after.format == "PNG"
        assert before.size == (size, size)
        assert after.size == (size, size)


def test_rebuild_existing_local_icons_emits_progress(tmp_path: Path) -> None:
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    _single_size_ico(icon_path, size=32)
    _desktop_ini(folder, rebuilt="false")
    _report, entries = collect_existing_local_icons([(folder, icon_path)])

    events: list[tuple[str, int, int]] = []
    report = rebuild_existing_local_icons(
        entries,
        progress_cb=lambda stage, current, total: events.append(
            (str(stage), int(current), int(total))
        ),
    )

    assert report.succeeded == 1
    assert events
    assert events[0][0] == "Rebuild icons"
    assert events[0][1] == 0
    assert events[-1][1] == events[-1][2]


def test_rebuild_existing_local_icons_supports_cancellation(tmp_path: Path) -> None:
    first = tmp_path / "GameA"
    second = tmp_path / "GameB"
    first.mkdir()
    second.mkdir()
    first_icon = first / "GameA.ico"
    second_icon = second / "GameB.ico"
    _single_size_ico(first_icon, size=32)
    _single_size_ico(second_icon, size=32)
    _desktop_ini(first, icon_name="GameA.ico", rebuilt="false")
    _desktop_ini(second, icon_name="GameB.ico", rebuilt="false")
    _report, entries = collect_existing_local_icons([(first, first_icon), (second, second_icon)])

    calls = {"count": 0}

    def _cancel_after_first() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(OperationCancelled):
        rebuild_existing_local_icons(entries, should_cancel=_cancel_after_first)
