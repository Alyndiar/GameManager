from pathlib import Path

from gamemanager.models import RootFolder
from gamemanager.services.operations import (
    build_move_plan,
    build_rename_plan,
    execute_move_plan,
    execute_rename_plan,
)


def _root(path: Path) -> RootFolder:
    return RootFolder(id=1, path=str(path), enabled=True, added_at="now")


def test_rename_plan_and_execution_with_conflict(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    src = root / "Game_Name.iso"
    src.write_text("data", encoding="utf-8")
    mtime_before = src.stat().st_mtime
    conflict_src = root / "Other.Name.iso"
    conflict_src.write_text("x", encoding="utf-8")
    (root / "Other Name.iso").write_text("existing", encoding="utf-8")

    plan = build_rename_plan([_root(root)])
    by_src = {p.src_path.name: p for p in plan}
    assert by_src["Game_Name.iso"].status == "ready"
    assert by_src["Other.Name.iso"].status == "conflict"

    report = execute_rename_plan(plan)
    assert report.succeeded == 1
    assert report.conflicts == 1
    renamed = root / "Game Name.iso"
    assert renamed.exists()
    assert abs(renamed.stat().st_mtime - mtime_before) < 0.0001


def test_move_plan_conflict_and_overwrite_action(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    src = root / "Pack_Game.iso"
    src.write_text("iso", encoding="utf-8")

    conflict_src = root / "Another.zip"
    conflict_src.write_text("new", encoding="utf-8")
    conflict_folder = root / "Another"
    conflict_folder.mkdir()
    (conflict_folder / "Another.zip").write_text("old", encoding="utf-8")

    plan = build_move_plan([_root(root)], {".iso", ".zip", ".rar", ".7z"})
    assert len(plan) == 2
    for item in plan:
        if item.src_path.name == "Another.zip":
            assert item.status == "conflict"
            item.selected_action = "overwrite"
        else:
            assert item.status == "ready"
            item.selected_action = "move"

    report = execute_move_plan(plan)
    assert report.succeeded == 2
    assert (root / "Pack Game" / "Pack_Game.iso").exists()
    assert (root / "Another" / "Another.zip").read_text(encoding="utf-8") == "new"

