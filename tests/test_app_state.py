from pathlib import Path
from datetime import datetime

import pytest

from gamemanager.app_state import AppState
from gamemanager.models import IconCandidate, InventoryItem


def test_add_root_success_and_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    root = tmp_path / "Jeux Téléchargés"
    root.mkdir()

    assert app.add_root(str(root)) == "added"
    assert app.add_root(str(root)) == "duplicate"
    assert len(app.list_roots()) == 1


def test_add_root_missing_path_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)

    with pytest.raises(ValueError, match="Folder does not exist"):
        app.add_root(str(tmp_path / "missing-folder"))


def test_sgdb_resource_preferences_persist_and_sanitize(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)

    order, enabled = app.save_sgdb_resource_preferences(
        ["heroes", "logos", "invalid", "icons"],
        {"heroes", "logos"},
    )
    assert order[:4] == ["heroes", "logos", "icons", "grids"]
    assert enabled == {"heroes", "logos"}

    loaded_order, loaded_enabled = app.sgdb_resource_preferences()
    assert loaded_order[:4] == ["heroes", "logos", "icons", "grids"]
    assert loaded_enabled == {"heroes", "logos"}


def test_get_or_fetch_game_infotip_caches_result(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    calls = {"count": 0}

    def _fake_fetch(name: str):
        calls["count"] += 1
        return ("One line description.", "steam")

    monkeypatch.setattr("gamemanager.app_state.fetch_game_infotip", _fake_fetch)
    first = app.get_or_fetch_game_infotip("Test Game")
    second = app.get_or_fetch_game_infotip("test game")
    assert first == "One line description."
    assert second == "One line description."
    assert calls["count"] == 1


def test_set_manual_folder_info_tip_updates_cache(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    folder = tmp_path / "Game"
    folder.mkdir()
    calls: list[tuple[str, str]] = []

    def _fake_read(_path):
        return ""

    def _fake_set(path, tip):
        calls.append((str(path), tip))
        return True

    monkeypatch.setattr("gamemanager.app_state.read_folder_info_tip", _fake_read)
    monkeypatch.setattr("gamemanager.app_state.set_folder_info_tip", _fake_set)

    ok = app.set_manual_folder_info_tip(
        str(folder),
        "Some Game",
        "Manual line.",
    )
    assert ok is True
    assert calls and calls[0][1] == "Manual line."
    cached = app.db.get_game_infotip("some game")
    assert cached is not None
    tip, source = cached
    assert tip == "Manual line."
    assert source == "manual"


def test_collect_icon_rebuild_entries_filters_to_local_icons(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)

    folder = tmp_path / "Game"
    folder.mkdir()
    local_icon = folder / "Game.ico"
    external = tmp_path / "external.ico"

    from PIL import Image

    Image.new("RGBA", (32, 32), (120, 80, 40, 255)).save(
        local_icon, format="ICO", sizes=[(32, 32)]
    )
    Image.new("RGBA", (32, 32), (120, 80, 40, 255)).save(
        external, format="ICO", sizes=[(32, 32)]
    )

    now = datetime.now()
    local_item = InventoryItem(
        root_id=1,
        root_path=str(tmp_path),
        source_label="R",
        full_name="Game",
        full_path=str(folder),
        is_dir=True,
        extension="",
        size_bytes=0,
        created_at=now,
        modified_at=now,
        cleaned_name="Game",
        scan_ts=now,
        icon_status="valid",
        folder_icon_path=str(local_icon),
    )
    external_item = InventoryItem(
        root_id=1,
        root_path=str(tmp_path),
        source_label="R",
        full_name="Game2",
        full_path=str(folder),
        is_dir=True,
        extension="",
        size_bytes=0,
        created_at=now,
        modified_at=now,
        cleaned_name="Game2",
        scan_ts=now,
        icon_status="valid",
        folder_icon_path=str(external),
    )

    report, findings = app.collect_icon_rebuild_entries([local_item, external_item])
    assert report.total == 1
    assert len(findings) == 1


def test_download_candidate_reads_local_file_path(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)
    image_path = tmp_path / "existing.ico"
    image_path.write_bytes(b"ICO_BYTES")
    candidate = IconCandidate(
        provider="Current Folder Icon",
        candidate_id="local-existing:test",
        title="Current",
        preview_url=str(image_path),
        image_url=str(image_path),
        width=0,
        height=0,
        has_alpha=True,
        source_url=str(image_path),
    )
    payload = app.download_candidate(candidate)
    assert payload == b"ICO_BYTES"


def test_candidate_preview_normalizes_avif_payload(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    app = AppState(db_path)

    from io import BytesIO
    from PIL import Image

    image = Image.new("RGBA", (128, 128), (100, 140, 220, 255))
    encoded = BytesIO()
    try:
        image.save(encoded, format="AVIF")
    except Exception:
        pytest.skip("AVIF encoder/runtime not available in this environment")
    avif_bytes = encoded.getvalue()

    monkeypatch.setattr("gamemanager.app_state.download_candidate_image", lambda _url: avif_bytes)
    candidate = IconCandidate(
        provider="Test",
        candidate_id="1",
        title="AVIF",
        preview_url="https://example.invalid/img.avif",
        image_url="https://example.invalid/img.avif",
        width=128,
        height=128,
        has_alpha=True,
        source_url="https://example.invalid",
    )
    preview = app.candidate_preview(candidate, icon_style="none", size=64)
    assert preview[:8] == b"\x89PNG\r\n\x1a\n"
