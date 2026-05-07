from __future__ import annotations

from datetime import datetime
import json

from PySide6.QtWidgets import QTableWidgetItem

from gamemanager.models import InventoryItem
from gamemanager.ui import main_window
from gamemanager.ui.main_window_inventory_ops import MainWindowInventoryOpsMixin


class _FakeState:
    def __init__(self):
        self.prefs: dict[str, str] = {}

    def get_ui_pref(self, key: str, default: str) -> str:
        return self.prefs.get(key, default)

    def set_ui_pref(self, key: str, value: str) -> None:
        self.prefs[key] = value

    def read_folder_icon_metadata(self, _path: str) -> dict[str, str]:
        return {}

    def available_store_names(self) -> list[str]:
        return ["Steam", "EGS"]


def _build_window(qtbot, monkeypatch, state: _FakeState):
    monkeypatch.setattr(main_window.MainWindow, "refresh_all", lambda self: None)
    monkeypatch.setattr(
        main_window.MainWindow,
        "_request_gpu_status_update",
        lambda self: None,
    )
    win = main_window.MainWindow(state)
    qtbot.addWidget(win)
    return win


def test_right_column_layout_restores_and_persists_order(qtbot, monkeypatch) -> None:
    state = _FakeState()
    state.prefs[main_window.RIGHT_COLUMN_LAYOUT_ORDER_PREF] = json.dumps(
        ["stores", "full_name", "cleaned_name", "modified_at", "created_at", "size_bytes", "source"]
    )
    state.prefs[main_window.RIGHT_COLUMN_LAYOUT_HIDDEN_PREF] = json.dumps(["source"])
    state.prefs[main_window.RIGHT_COLUMN_LAYOUT_WIDTHS_PREF] = json.dumps({"full_name": 320})
    win = _build_window(qtbot, monkeypatch, state)

    header = win.right_table.horizontalHeader()
    assert header.visualIndex(win._right_column_index_by_field("stores")) == 0
    assert win.right_table.isColumnHidden(win._right_column_index_by_field("source")) is True
    assert win.right_table.columnWidth(win._right_column_index_by_field("full_name")) >= 320
    assert win.right_table.isColumnHidden(0) is False

    name_logical = win._right_column_index_by_field("full_name")
    current_visual = header.visualIndex(name_logical)
    if current_visual != 0:
        header.moveSection(current_visual, 0)
    saved = json.loads(state.prefs[main_window.RIGHT_COLUMN_LAYOUT_ORDER_PREF])
    assert saved[0] == "full_name"


def test_double_click_autosize_and_name_column_lock(qtbot, monkeypatch) -> None:
    state = _FakeState()
    win = _build_window(qtbot, monkeypatch, state)

    name_col = win._right_column_index_by_field("full_name")
    win.right_table.setRowCount(1)
    win.right_table.setItem(0, name_col, QTableWidgetItem("Very Long Game Name For Auto Fit Testing"))
    win.right_table.setColumnWidth(name_col, 40)
    before = win.right_table.columnWidth(name_col)
    win._on_right_header_handle_double_clicked(name_col)
    after = win.right_table.columnWidth(name_col)
    assert after > before

    for idx in range(1, len(win._right_columns)):
        win._toggle_right_column_visibility(idx, False)
    assert win._visible_right_columns_count() == 1
    assert win._toggle_right_column_visibility(0, False) is False
    assert win.right_table.isColumnHidden(0) is False


def test_store_accounts_button_opens_dialog(qtbot, monkeypatch) -> None:
    state = _FakeState()
    win = _build_window(qtbot, monkeypatch, state)
    captured = {"opened": 0}

    class _FakeDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            captured["opened"] += 1
            return 0

    win._store_accounts_dialog_cls = _FakeDialog
    win._on_store_accounts()
    assert captured["opened"] == 1


def test_store_filter_selector_contains_all_any_none(qtbot, monkeypatch) -> None:
    state = _FakeState()
    win = _build_window(qtbot, monkeypatch, state)
    labels = [win.right_store_filter_combo.itemText(i) for i in range(win.right_store_filter_combo.count())]
    assert labels[:3] == ["All", "Any", "None"]
    assert win.right_store_filter_combo.itemData(0) == ""
    assert win.right_store_filter_combo.itemData(1) == "__any__"
    assert win.right_store_filter_combo.itemData(2) == "__none__"


def test_right_sort_controls_stay_linked_with_header_sort(qtbot, monkeypatch) -> None:
    state = _FakeState()
    win = _build_window(qtbot, monkeypatch, state)

    name_col = win._right_column_index_by_field("full_name")
    assert name_col >= 0
    win._on_right_header_clicked(name_col)
    assert int(win.right_sort_combo.currentData()) == name_col
    assert bool(win.right_sort_order_combo.currentData()) is True

    descending_idx = win.right_sort_order_combo.findData(False)
    assert descending_idx >= 0
    win.right_sort_order_combo.setCurrentIndex(descending_idx)
    assert win._right_sort_ascending is False


def _inv(path: str, *, stores: list[str], primary: str = "") -> InventoryItem:
    now = datetime(2026, 1, 1, 12, 0, 0)
    return InventoryItem(
        root_id=1,
        root_path="C:\\Games",
        source_label="C:",
        full_name=path.split("\\")[-1],
        full_path=path,
        is_dir=True,
        extension="",
        size_bytes=0,
        created_at=now,
        modified_at=now,
        cleaned_name=path.split("\\")[-1],
        scan_ts=now,
        owned_stores=list(stores),
        primary_store=primary or None,
    )


def test_store_filter_any_and_none_semantics() -> None:
    items = [
        _inv("C:\\Games\\A", stores=["Steam"], primary="Steam"),
        _inv("C:\\Games\\B", stores=[], primary=""),
        _inv("C:\\Games\\C", stores=[], primary="GOG"),
    ]
    any_rows = MainWindowInventoryOpsMixin._filter_by_owned_store(items, "__any__")
    none_rows = MainWindowInventoryOpsMixin._filter_by_owned_store(items, "__none__")
    assert [row.full_path for row in any_rows] == ["C:\\Games\\A", "C:\\Games\\C"]
    assert [row.full_path for row in none_rows] == ["C:\\Games\\B"]
