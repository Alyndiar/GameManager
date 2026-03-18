from pathlib import Path

from gamemanager import main as gm_main


def test_default_db_path_moves_legacy_db_and_cache(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "appdata"
    legacy_dir = appdata / "GameBackupManager"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "manager.db").write_bytes(b"legacy-db")
    legacy_cache = legacy_dir / "cache" / "candidate_previews"
    legacy_cache.mkdir(parents=True)
    (legacy_cache / "a.bin").write_bytes(b"x")

    target_dir = tmp_path / "project_data"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("GAMEMANAGER_DATA_DIR", str(target_dir))

    db_path = gm_main._default_db_path()

    assert db_path == target_dir / "manager.db"
    assert db_path.exists()
    assert db_path.read_bytes() == b"legacy-db"
    assert (target_dir / "cache" / "candidate_previews" / "a.bin").exists()
    assert not legacy_dir.exists()


def test_default_db_path_removes_legacy_duplicate_db(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "appdata"
    legacy_dir = appdata / "GameBackupManager"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "manager.db").write_bytes(b"same-db")

    target_dir = tmp_path / "project_data"
    target_dir.mkdir(parents=True)
    (target_dir / "manager.db").write_bytes(b"same-db")

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("GAMEMANAGER_DATA_DIR", str(target_dir))

    db_path = gm_main._default_db_path()

    assert db_path == target_dir / "manager.db"
    assert db_path.exists()
    assert not legacy_dir.exists()
