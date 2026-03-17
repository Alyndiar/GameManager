from pathlib import Path

from gamemanager.services.elevated_delete import delete_path


def test_delete_path_removes_file_and_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    delete_path(str(file_path))
    assert not file_path.exists()

    dir_path = tmp_path / "dir"
    dir_path.mkdir()
    nested = dir_path / "nested.bin"
    nested.write_bytes(b"123")
    delete_path(str(dir_path))
    assert not dir_path.exists()

