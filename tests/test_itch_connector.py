from __future__ import annotations

import json
from pathlib import Path

from gamemanager.services.storefronts.itch_connector import (
    ItchConnector,
    _ItchOwnedSnapshot,
)


def test_itch_connect_selects_requested_profile(monkeypatch, tmp_path) -> None:
    connector = ItchConnector()
    exe = tmp_path / "butler.exe"
    db = tmp_path / "butler.db"
    exe.write_text("x", encoding="utf-8")
    db.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_executable_path",
        lambda: exe,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_database_path",
        lambda: db,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._list_profiles",
        lambda **_kwargs: [
            {
                "id": "10",
                "username": "alpha",
                "display_name": "Alpha",
                "last_connected": "2026-03-01T10:00:00Z",
            },
            {
                "id": "20",
                "username": "beta",
                "display_name": "Beta",
                "last_connected": "2026-04-01T10:00:00Z",
            },
        ],
    )

    result = connector.connect({"account_id": "10"})
    assert result.success is True
    assert result.account_id == "10"
    assert result.display_name == "Alpha"
    assert result.auth_kind == "launcher_profile_owned_keys"


def test_itch_connect_fails_without_logged_profile(monkeypatch, tmp_path) -> None:
    connector = ItchConnector()
    exe = tmp_path / "butler.exe"
    db = tmp_path / "butler.db"
    exe.write_text("x", encoding="utf-8")
    db.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_executable_path",
        lambda: exe,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_database_path",
        lambda: db,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._list_profiles",
        lambda **_kwargs: [],
    )

    result = connector.connect({})
    assert result.success is False
    assert result.status == "missing_profile"


def test_itch_refresh_merges_owned_keys_and_caves(monkeypatch, tmp_path) -> None:
    connector = ItchConnector()
    exe = tmp_path / "butler.exe"
    db = tmp_path / "butler.db"
    exe.write_text("x", encoding="utf-8")
    db.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_executable_path",
        lambda: exe,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_database_path",
        lambda: db,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._list_profiles",
        lambda **_kwargs: [
            {
                "id": "10",
                "username": "alpha",
                "display_name": "Alpha",
                "last_connected": "2026-04-01T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._fetch_owned_snapshot",
        lambda *_args, **_kwargs: _ItchOwnedSnapshot(
            owned_keys=[
                {
                    "id": "dk-1",
                    "gameId": 100,
                    "game": {
                        "id": 100,
                        "title": "Portal 2",
                        "url": "https://foo.itch.io/portal-2",
                        "coverUrl": "https://cdn.example/portal2.png",
                    },
                }
            ],
            caves=[
                {
                    "id": "cave-100",
                    "game": {
                        "id": 100,
                        "title": "Portal 2",
                        "url": "https://foo.itch.io/portal-2",
                    },
                    "installInfo": {"installFolder": "D:/Games/Portal2"},
                },
                {
                    "id": "cave-200",
                    "game": {
                        "id": 200,
                        "title": "A Short Hike",
                        "url": "https://bar.itch.io/a-short-hike",
                    },
                    "installInfo": {"installFolder": "D:/Games/AShortHike"},
                },
            ],
        ),
    )

    rows = connector.refresh_entitlements("10")
    by_id = {row.entitlement_id: row for row in rows}
    assert set(by_id.keys()) == {"100", "200"}
    assert by_id["100"].is_installed is True
    assert by_id["100"].install_path == "D:/Games/Portal2"
    assert by_id["100"].store_game_id == "https://foo.itch.io/portal-2"
    assert by_id["200"].is_installed is True
    metadata = json.loads(by_id["100"].metadata_json)
    assert metadata["key_id"] == "dk-1"


def test_itch_status_reports_unavailable_without_runtime(monkeypatch) -> None:
    connector = ItchConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_executable_path",
        lambda: Path(""),
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.itch_connector._butler_database_path",
        lambda: Path(""),
    )
    status = connector.status("10")
    assert status.available is False
    assert status.connected is False
