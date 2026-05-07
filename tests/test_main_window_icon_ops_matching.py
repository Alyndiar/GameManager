from datetime import datetime

from PySide6.QtWidgets import QMessageBox

from gamemanager.models import InventoryItem, SgdbGameCandidate
from gamemanager.ui.main_window_icon_ops import MainWindowIconOpsMixin


def _inventory_item(name: str) -> InventoryItem:
    now = datetime(2026, 1, 1, 12, 0, 0)
    return InventoryItem(
        root_id=1,
        root_path="C:\\Games",
        source_label="C:",
        full_name=name,
        full_path=f"C:\\Games\\{name}",
        is_dir=True,
        extension="",
        size_bytes=0,
        created_at=now,
        modified_at=now,
        cleaned_name=name,
        scan_ts=now,
    )


class _Ops(MainWindowIconOpsMixin):
    pass


class _FlowOps(MainWindowIconOpsMixin):
    def __init__(self) -> None:
        self._sgdb_picker_flow_state: dict[str, object] | None = None
        self.interrupted = False

    def _interrupt_active_sgdb_picker_flow(self) -> None:
        self.interrupted = True


def test_unique_exact_normalized_full_confidence_candidate_picks_single_exact() -> None:
    ops = _Ops()
    entry = _inventory_item("Aaero-2")
    candidates = [
        SgdbGameCandidate(
            game_id=1,
            title="Aaero 2 Soundtrack",
            confidence=1.0,
            evidence=["x"],
        ),
        SgdbGameCandidate(
            game_id=2,
            title="Aaero 2",
            confidence=1.0,
            evidence=["x"],
        ),
    ]
    selected = ops._unique_exact_normalized_full_confidence_candidate(entry, candidates)
    assert selected is not None
    assert selected.game_id == 2


def test_unique_exact_normalized_full_confidence_candidate_none_when_ambiguous() -> None:
    ops = _Ops()
    entry = _inventory_item("Portal")
    candidates = [
        SgdbGameCandidate(
            game_id=1,
            title="Portal",
            confidence=1.0,
            evidence=["x"],
        ),
        SgdbGameCandidate(
            game_id=2,
            title="Portal",
            confidence=1.0,
            evidence=["x"],
        ),
    ]
    selected = ops._unique_exact_normalized_full_confidence_candidate(entry, candidates)
    assert selected is None


def test_resolve_candidate_with_steam_appid_uses_store_ids_map() -> None:
    candidate = SgdbGameCandidate(
        game_id=1,
        title="Portal",
        confidence=1.0,
        evidence=["x"],
        steam_appid=None,
        identity_store=None,
        identity_store_id=None,
        store_ids={"Steam": "620"},
    )
    selected, appid = _Ops._resolve_candidate_with_steam_appid(None, candidate)
    assert selected is candidate
    assert appid == "620"


def test_confirm_interrupt_active_sgdb_picker_flow_returns_false_on_no(monkeypatch) -> None:
    ops = _FlowOps()
    ops._sgdb_picker_flow_state = {"flow_title": "Assign SteamID"}
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )
    allowed = ops._confirm_interrupt_active_sgdb_picker_flow("Recheck IDs and Stores")
    assert allowed is False
    assert ops.interrupted is False


def test_confirm_interrupt_active_sgdb_picker_flow_interrupts_on_yes(monkeypatch) -> None:
    ops = _FlowOps()
    ops._sgdb_picker_flow_state = {"flow_title": "Assign SteamID"}
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    allowed = ops._confirm_interrupt_active_sgdb_picker_flow("Recheck IDs and Stores")
    assert allowed is True
    assert ops.interrupted is True
