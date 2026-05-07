from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from gamemanager.models import SgdbGameCandidate
from gamemanager.ui.dialogs.steamgriddb_target_picker import SgdbTargetPickerDialog


def test_cancel_all_sets_flag(qtbot, monkeypatch) -> None:
    dialog = SgdbTargetPickerDialog(
        folder_name="Portal",
        folder_path="",
        candidates=[],
        drift_reasons=[],
        icon_path="",
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    dialog._on_cancel_all()
    assert dialog.cancel_all_requested is True


def test_open_game_folder_launches_explorer(qtbot, monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    dialog = SgdbTargetPickerDialog(
        folder_name="Portal",
        folder_path=str(tmp_path),
        candidates=[
            SgdbGameCandidate(
                game_id=620,
                title="Portal",
                confidence=1.0,
                evidence=["Exact Steam AppID 620"],
                steam_appid="620",
            )
        ],
        drift_reasons=[],
        icon_path="",
    )
    qtbot.addWidget(dialog)

    monkeypatch.setattr(
        "gamemanager.ui.dialogs.steamgriddb_target_picker.subprocess.Popen",
        lambda args: calls.append(list(args)),
    )
    dialog._on_open_game_folder()
    assert calls
    assert calls[0][0].casefold() == "explorer"


def test_store_url_uses_store_ids_when_identity_missing() -> None:
    candidate = SgdbGameCandidate(
        game_id=1,
        title="Portal",
        confidence=1.0,
        evidence=["x"],
        steam_appid=None,
        identity_store=None,
        identity_store_id=None,
        store_ids={"GOG": "portal_2"},
    )
    url = SgdbTargetPickerDialog._steam_url_for_candidate(candidate)
    assert "gog.com" in url
