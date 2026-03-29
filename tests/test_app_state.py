from pathlib import Path
from datetime import datetime

import pytest

from gamemanager.app_state import AppState
from gamemanager.models import IconCandidate, IconRebuildEntry, InventoryItem, SgdbGameCandidate


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


def test_sgdb_binding_roundtrip(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    app.save_sgdb_binding(
        str(folder),
        12345,
        "Portal",
        0.91,
        ["Name match 'Portal'"],
    )
    binding = app.get_sgdb_binding(str(folder))
    assert binding is not None
    assert binding.game_id == 12345
    assert binding.game_name == "Portal"
    assert binding.last_confidence == pytest.approx(0.91)


def test_sgdb_upload_history_lookup(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    fp = "abc123"
    assert app.was_uploaded_to_sgdb(str(folder), 5, fp) is False
    app.record_sgdb_upload_event(str(folder), 5, fp, "uploaded", "ok")
    assert app.was_uploaded_to_sgdb(str(folder), 5, fp) is True


def test_record_assigned_icon_source_updates_desktop_metadata(tmp_path: Path, monkeypatch) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / "Game.ico").write_bytes(b"ICO")
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\Game.ico,0\nFlags=0\nRebuilt=true\n",
        encoding="utf-8-sig",
    )

    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)

    changed = app.record_assigned_icon_source(
        folder_path=str(folder),
        source_kind="sgdb_raw",
        source_provider="SteamGridDB",
        source_candidate_id="icons:22",
        source_game_id="4422",
        source_url="https://example/icon.png",
        source_fingerprint256="deadbeef",
        source_confidence=1.0,
    )
    assert changed is True
    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "sgdb_raw"
    assert metadata.get("SourceProvider") == "SteamGridDB"


def test_upload_folder_icon_to_sgdb_skips_when_already_uploaded(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (12, 34, 200, 255)).save(icon, format="ICO", sizes=[(256, 256)])
    fingerprint = app.icon_fingerprint256(str(icon))
    app.record_sgdb_upload_event(str(folder), 123, fingerprint, "uploaded", "ok")

    report = app.upload_folder_icon_to_sgdb(
        folder_path=str(folder),
        icon_path=str(icon),
        game=SgdbGameCandidate(
            game_id=123,
            title="Test",
            confidence=1.0,
            evidence=["test"],
        ),
    )
    assert report.skipped == 1


def test_upload_folder_icon_to_sgdb_skips_when_source_already_sgdb(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (12, 34, 200, 255)).save(icon, format="ICO", sizes=[(256, 256)])
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n\n"
        "[GameManager.Icon]\n"
        "SourceKind=sgdb_raw\n"
        "SourceProvider=SteamGridDB\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)
    monkeypatch.setattr(
        "gamemanager.app_state.upload_icon_to_sgdb_in_subprocess",
        lambda settings, game_id, icon_path: (_ for _ in ()).throw(
            AssertionError("upload_icon_to_sgdb_in_subprocess should not be called")
        ),
    )

    report = app.upload_folder_icon_to_sgdb(
        folder_path=str(folder),
        icon_path=str(icon),
        game=SgdbGameCandidate(
            game_id=123,
            title="Test",
            confidence=1.0,
            evidence=["test"],
        ),
    )
    assert report.skipped == 1
    assert any("already SteamGridDB" in line for line in report.details)


def test_upload_folder_icon_to_sgdb_marks_source_as_sgdb_on_success(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (12, 34, 200, 255)).save(icon, format="ICO", sizes=[(256, 256)])
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\nIconResource=.\\Game.ico,0\nFlags=0\nRebuilt=true\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)
    monkeypatch.setattr(
        "gamemanager.app_state.upload_icon_to_sgdb_in_subprocess",
        lambda settings, game_id, icon_path: {"success": True},
    )

    report = app.upload_folder_icon_to_sgdb(
        folder_path=str(folder),
        icon_path=str(icon),
        game=SgdbGameCandidate(
            game_id=123,
            title="Test",
            confidence=0.87,
            evidence=["test"],
        ),
    )
    assert report.succeeded == 1
    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "sgdb_raw"
    assert metadata.get("SourceProvider") == "SteamGridDB"
    assert metadata.get("SourceGameId") == "123"


def test_record_assigned_icon_source_overwrites_web_variant_with_internet(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    (folder / "Game.ico").write_bytes(b"ICO")
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n\n"
        "[GameManager.Icon]\n"
        "SourceKind=web\n"
        "SourceProvider=Downloaded Image\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)

    changed = app.record_assigned_icon_source(
        folder_path=str(folder),
        source_kind="web",
        source_provider="Internet",
        source_candidate_id="x",
    )
    assert changed is True
    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceProvider") == "Internet"


def test_backfill_missing_icon_sources_defaults_to_internet(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (90, 120, 180, 255)).save(
        icon_path, format="ICO", sizes=[(256, 256)]
    )
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)
    monkeypatch.setattr(
        "gamemanager.app_state.probe_icon_source_in_subprocess",
        lambda **kwargs: {
            "status": "ok",
            "source_kind": "web",
            "source_provider": "Internet",
            "source_confidence": 0.0,
            "source_note": "fallback",
            "source_fingerprint256": "abc123",
        },
    )

    now = datetime.now()
    item = InventoryItem(
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
        folder_icon_path=str(icon_path),
    )

    report = app.backfill_missing_icon_sources([item])
    assert report.succeeded == 1
    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "web"
    assert metadata.get("SourceProvider") == "Internet"


def test_backfill_missing_icon_sources_rechecks_when_source_exists_without_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (90, 120, 180, 255)).save(
        icon_path, format="ICO", sizes=[(256, 256)]
    )
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n\n"
        "[GameManager.Icon]\n"
        "SourceKind=local\n"
        "SourceProvider=Local File\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)
    probe_calls = {"count": 0}

    def _probe(**kwargs):
        probe_calls["count"] += 1
        return {"status": "ok"}

    monkeypatch.setattr("gamemanager.app_state.probe_icon_source_in_subprocess", _probe)

    now = datetime.now()
    item = InventoryItem(
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
        folder_icon_path=str(icon_path),
    )

    report = app.backfill_missing_icon_sources([item])
    assert report.succeeded == 1
    assert report.skipped == 0
    assert probe_calls["count"] == 1
    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "web"
    assert metadata.get("SourceProvider") == "Internet"
    assert metadata.get("SourceBackfillFingerprint256")


def test_backfill_missing_icon_sources_skips_when_unchanged_since_last_backfill(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (90, 120, 180, 255)).save(
        icon_path, format="ICO", sizes=[(256, 256)]
    )
    current_fp = app.icon_fingerprint256(str(icon_path))
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n\n"
        "[GameManager.Icon]\n"
        "SourceKind=web\n"
        "SourceProvider=Internet\n"
        f"SourceFingerprint256={current_fp}\n"
        f"SourceBackfillFingerprint256={current_fp}\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)
    probe_calls = {"count": 0}

    def _probe(**kwargs):
        probe_calls["count"] += 1
        return {"status": "ok"}

    monkeypatch.setattr("gamemanager.app_state.probe_icon_source_in_subprocess", _probe)

    now = datetime.now()
    item = InventoryItem(
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
        folder_icon_path=str(icon_path),
    )

    report = app.backfill_missing_icon_sources([item])
    assert report.succeeded == 0
    assert report.skipped == 1
    assert probe_calls["count"] == 0


def test_backfill_missing_icon_sources_skips_when_source_is_sgdb(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (90, 120, 180, 255)).save(
        icon_path, format="ICO", sizes=[(256, 256)]
    )
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n\n"
        "[GameManager.Icon]\n"
        "SourceKind=sgdb_raw\n"
        "SourceProvider=SteamGridDB\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)
    probe_calls = {"count": 0}

    def _probe(**kwargs):
        probe_calls["count"] += 1
        return {"status": "ok"}

    monkeypatch.setattr("gamemanager.app_state.probe_icon_source_in_subprocess", _probe)

    now = datetime.now()
    item = InventoryItem(
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
        folder_icon_path=str(icon_path),
    )

    report = app.backfill_missing_icon_sources([item])
    assert report.succeeded == 0
    assert report.skipped == 1
    assert probe_calls["count"] == 0


def test_rebuild_existing_icons_syncs_source_fingerprint_without_changing_source(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    folder = tmp_path / "Game"
    folder.mkdir()
    icon_path = folder / "Game.ico"
    from PIL import Image

    Image.new("RGBA", (256, 256), (90, 120, 180, 255)).save(
        icon_path, format="ICO", sizes=[(256, 256)]
    )
    (folder / "desktop.ini").write_text(
        "[.ShellClassInfo]\n"
        "IconResource=.\\Game.ico,0\n"
        "Flags=0\n"
        "Rebuilt=true\n\n"
        "[GameManager.Icon]\n"
        "SourceKind=web\n"
        "SourceProvider=Internet\n"
        "SourceFingerprint256=deadbeef\n"
        "SourceBackfillFingerprint256=deadbeef\n",
        encoding="utf-8-sig",
    )
    from gamemanager.services import folder_icons

    monkeypatch.setattr(folder_icons, "_run_attrib", lambda args: None)
    monkeypatch.setattr(folder_icons, "_shell_refresh", lambda path: None)

    from gamemanager.models import OperationReport

    def _fake_rebuild_report(
        entries,
        size_improvements=None,
        *,
        force_rebuild=False,
        create_backups=True,
        progress_cb=None,
        should_cancel=None,
        on_rebuilt=None,
    ):
        if on_rebuilt is not None:
            first = entries[0]
            on_rebuilt(first.folder_path, first.icon_path)
        return OperationReport(total=1, succeeded=1)

    monkeypatch.setattr(
        "gamemanager.app_state.rebuild_existing_local_icons",
        _fake_rebuild_report,
    )
    entry = IconRebuildEntry(
        folder_path=str(folder),
        icon_path=str(icon_path),
        already_rebuilt=True,
        summary="Already rebuilt",
    )
    report = app.rebuild_existing_icons([entry], force_rebuild=True, create_backups=False)
    assert report.succeeded == 1
    metadata = app.read_folder_icon_metadata(str(folder))
    assert metadata.get("SourceKind") == "web"
    assert metadata.get("SourceProvider") == "Internet"
    current_fp = app.icon_fingerprint256(str(icon_path))
    assert metadata.get("SourceFingerprint256") == current_fp
    assert metadata.get("SourceBackfillFingerprint256") == current_fp
