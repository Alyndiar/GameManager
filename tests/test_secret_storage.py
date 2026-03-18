from pathlib import Path

from gamemanager import app_state as app_state_module
from gamemanager.app_state import AppState
from gamemanager.services.icon_sources import IconSearchSettings


def test_icon_search_settings_migrates_legacy_plaintext_keys(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    app.set_ui_pref("steamgriddb_api_key", "legacy-steam")
    app.set_ui_pref("iconfinder_api_key", "legacy-iconfinder")

    monkeypatch.setattr(app_state_module, "get_secret", lambda key: "")
    saved: dict[str, str] = {}

    def _set_secret(key: str, value: str) -> bool:
        saved[key] = value
        return True

    monkeypatch.setattr(app_state_module, "set_secret", _set_secret)

    settings = app.icon_search_settings()

    assert settings.steamgriddb_api_key == "legacy-steam"
    assert settings.iconfinder_api_key == "legacy-iconfinder"
    assert saved["steamgriddb_api_key"] == "legacy-steam"
    assert saved["iconfinder_api_key"] == "legacy-iconfinder"
    assert app.get_ui_pref("steamgriddb_api_key", "<missing>") == ""
    assert app.get_ui_pref("iconfinder_api_key", "<missing>") == ""


def test_save_icon_search_settings_stores_keys_in_secret_store(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    app.set_ui_pref("steamgriddb_api_key", "plain")
    app.set_ui_pref("iconfinder_api_key", "plain")

    saved: dict[str, str] = {}

    def _set_secret(key: str, value: str) -> bool:
        saved[key] = value
        return True

    monkeypatch.setattr(app_state_module, "set_secret", _set_secret)
    monkeypatch.setattr(app_state_module, "delete_secret", lambda key: True)

    app.save_icon_search_settings(
        IconSearchSettings(
            steamgriddb_enabled=True,
            steamgriddb_api_key="s-key",
            steamgriddb_api_base="https://steam.example",
            iconfinder_enabled=True,
            iconfinder_api_key="i-key",
            iconfinder_api_base="https://icon.example",
        )
    )

    assert saved["steamgriddb_api_key"] == "s-key"
    assert saved["iconfinder_api_key"] == "i-key"
    assert app.get_ui_pref("steamgriddb_api_key", "<missing>") == ""
    assert app.get_ui_pref("iconfinder_api_key", "<missing>") == ""


def test_save_icon_search_settings_raises_when_secret_store_fails(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    monkeypatch.setattr(app_state_module, "set_secret", lambda key, value: False)
    monkeypatch.setattr(app_state_module, "delete_secret", lambda key: True)

    raised = False
    try:
        app.save_icon_search_settings(
            IconSearchSettings(
                steamgriddb_enabled=True,
                steamgriddb_api_key="s-key",
                steamgriddb_api_base="https://steam.example",
                iconfinder_enabled=True,
                iconfinder_api_key="i-key",
                iconfinder_api_base="https://icon.example",
            )
        )
    except RuntimeError:
        raised = True
    assert raised
