from __future__ import annotations

import json
from pathlib import Path

from gamemanager.app_state import AppState


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._payload


def test_resolve_sgdb_game_by_store_id_steam_uses_platform_lookup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = AppState(tmp_path / "db.sqlite3")

    class _Details:
        game_id = 123
        title = "Portal"
        steam_appid = "620"

    monkeypatch.setattr(
        "gamemanager.app_state.sgdb_resolve_game_by_platform_id",
        lambda *_args, **_kwargs: _Details(),
    )
    candidate = app.resolve_sgdb_game_by_store_id("Steam", "620")
    assert candidate.game_id == 123
    assert candidate.title == "Portal"
    assert candidate.steam_appid == "620"
    assert candidate.identity_store == "Steam"
    assert candidate.identity_store_id == "620"


def test_resolve_sgdb_game_by_store_id_steam_fallback_without_sgdb_mapping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = AppState(tmp_path / "db.sqlite3")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("not found")

    monkeypatch.setattr(
        "gamemanager.app_state.sgdb_resolve_game_by_platform_id",
        _raise,
    )
    payload = json.dumps(
        {
            "620": {
                "success": True,
                "data": {"name": "Portal"},
            }
        }
    ).encode("utf-8")
    monkeypatch.setattr(
        "gamemanager.app_state.request.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(payload),
    )

    candidate = app.resolve_sgdb_game_by_store_id("Steam", "620")
    assert candidate.game_id == 0
    assert candidate.title == "Portal"
    assert candidate.steam_appid == "620"
    assert candidate.identity_store == "Steam"
    assert candidate.identity_store_id == "620"
