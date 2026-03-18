from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gamemanager.models import (
    IconCandidate,
    InventoryItem,
    MovePlanItem,
    RenamePlanItem,
    TagCandidate,
)


@dataclass(slots=True)
class TagReviewResult:
    decisions: dict[str, str]
    display_map: dict[str, str]


@dataclass(slots=True)
class IconPickerResult:
    candidate: IconCandidate | None
    local_image_path: str | None
    info_tip: str
    circular_ring: bool


@dataclass(slots=True)
class IconProviderSettingsResult:
    steamgriddb_enabled: bool
    steamgriddb_api_key: str
    steamgriddb_api_base: str
    iconfinder_enabled: bool
    iconfinder_api_key: str
    iconfinder_api_base: str


class TagReviewDialog(QDialog):
    def __init__(
        self,
        candidates: list[TagCandidate],
        approved_tags: set[str],
        non_tags: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Tag Finder")
        self._combos: dict[str, QComboBox] = {}
        self._display: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Review suffix tag candidates and classify as approved or non-tag.")
        )
        self.table = QTableWidget(len(candidates), 4, self)
        self.table.setHorizontalHeaderLabels(["Tag", "Count", "Example", "Decision"])
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        for row, candidate in enumerate(candidates):
            canonical = candidate.canonical_tag
            self._display[canonical] = candidate.observed_tag

            self.table.setItem(row, 0, QTableWidgetItem(candidate.observed_tag))
            self.table.setItem(row, 1, QTableWidgetItem(str(candidate.count)))
            self.table.setItem(row, 2, QTableWidgetItem(candidate.example_name))
            combo = QComboBox(self.table)
            combo.addItems(["ignore", "approved", "non_tag"])
            if canonical in approved_tags:
                combo.setCurrentText("approved")
            elif canonical in non_tags:
                combo.setCurrentText("non_tag")
            else:
                combo.setCurrentText("ignore")
            self.table.setCellWidget(row, 3, combo)
            self._combos[canonical] = combo
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def result_payload(self) -> TagReviewResult:
        decisions: dict[str, str] = {}
        for canonical, combo in self._combos.items():
            choice = combo.currentText()
            if choice in {"approved", "non_tag"}:
                decisions[canonical] = choice
        return TagReviewResult(decisions=decisions, display_map=self._display)


class CleanupPreviewDialog(QDialog):
    def __init__(self, plan_items: list[RenamePlanItem], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Cleanup Preview")
        self.plan_items = plan_items
        self.visible_items = [item for item in plan_items if item.status != "unchanged"]
        self._action_by_item_id: dict[int, QComboBox] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Safe cleanup will rename non-conflicting root-level items on disk. "
                "Conflicts are flagged for manual rename."
            )
        )

        self.table = QTableWidget(len(self.visible_items), 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Proposed Name", "Destination", "Status"]
        )
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        for row, item in enumerate(self.visible_items):
            status = item.status
            self.table.setItem(row, 0, QTableWidgetItem(str(item.src_path)))
            self.table.setItem(row, 1, QTableWidgetItem(item.proposed_name))
            self.table.setItem(row, 2, QTableWidgetItem(str(item.dst_path)))
            if status == "ready":
                combo = QComboBox(self.table)
                combo.addItems(["Rename", "Skip"])
                combo.setCurrentText("Rename")
                self.table.setCellWidget(row, 3, combo)
                self._action_by_item_id[id(item)] = combo
            else:
                self.table.setItem(
                    row, 3, QTableWidgetItem("Conflict - manual rename required")
                )
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        self.apply_btn = QPushButton("Apply Safe Renames")
        self.cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def safe_items(self) -> list[RenamePlanItem]:
        selected: list[RenamePlanItem] = []
        for item in self.plan_items:
            if item.status != "ready":
                continue
            combo = self._action_by_item_id.get(id(item))
            if combo is None or combo.currentText() == "Rename":
                selected.append(item)
        return selected


class MovePreviewDialog(QDialog):
    def __init__(self, plan_items: list[MovePlanItem], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Archive Move Preview")
        self.plan_items = plan_items
        self._widgets: list[tuple[QComboBox, QLineEdit]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Preview archive/ISO moves into same-name subfolders. "
                "Conflicts default to skip."
            )
        )

        self.table = QTableWidget(len(plan_items), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Destination", "Status", "Action", "Manual Name"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        for row, item in enumerate(plan_items):
            self.table.setItem(row, 0, QTableWidgetItem(str(item.src_path)))
            self.table.setItem(row, 1, QTableWidgetItem(str(item.dst_path)))
            status_text = (
                "Ready to move"
                if item.status == "ready"
                else f"Conflict: {item.conflict_type or 'unknown'}"
            )
            self.table.setItem(row, 2, QTableWidgetItem(status_text))

            combo = QComboBox(self.table)
            if item.status == "ready":
                combo.addItems(["move", "skip"])
                combo.setCurrentText("move")
            else:
                combo.addItems(["skip", "overwrite", "rename", "delete_destination"])
                combo.setCurrentText("skip")
            self.table.setCellWidget(row, 3, combo)

            line = QLineEdit(self.table)
            line.setPlaceholderText("Only for action=rename")
            line.setEnabled(False)
            combo.currentTextChanged.connect(
                lambda text, edit=line: edit.setEnabled(text == "rename")
            )
            self.table.setCellWidget(row, 4, line)
            self._widgets.append((combo, line))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        self.apply_btn = QPushButton("Execute Moves")
        self.cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_btn.clicked.connect(self._validate_then_accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _validate_then_accept(self) -> None:
        for idx, (combo, line) in enumerate(self._widgets):
            if combo.currentText() == "rename" and not line.text().strip():
                QMessageBox.warning(
                    self,
                    "Missing Manual Name",
                    f"Row {idx + 1} uses action 'rename' but manual name is empty.",
                )
                return
        self.accept()

    def applied_items(self) -> list[MovePlanItem]:
        for idx, item in enumerate(self.plan_items):
            combo, line = self._widgets[idx]
            item.selected_action = combo.currentText()
            manual = line.text().strip()
            item.manual_name = manual if manual else None
        return self.plan_items


class IconPickerDialog(QDialog):
    def __init__(
        self,
        folder_name: str,
        candidates: list[IconCandidate],
        preview_loader,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Assign Folder Icon - {folder_name}")
        self.candidates = candidates
        self._preview_loader = preview_loader
        self._local_image_path: str | None = None
        self._preview_pix_cache: dict[tuple[int, int, bool], QPixmap] = {}
        self._hover_row: int | None = None
        self._hover_popup = QLabel(
            None,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self._hover_popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._hover_popup.setFrameStyle(QFrame.Shape.Panel | QFrame.Shadow.Plain)
        self._hover_popup.setLineWidth(1)
        self._hover_popup.setStyleSheet("background-color: #1c1c1c; padding: 4px;")

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Pick one candidate icon, or choose a local image. "
                "Circular + ring styling is enabled by default."
            )
        )

        self.table = QTableWidget(len(candidates), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Preview", "Title", "Provider", "Size", "Source"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setIconSize(QSize(64, 64))
        self.table.setMouseTracking(True)
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        for row, candidate in enumerate(candidates):
            preview_item = QTableWidgetItem("")
            preview_item.setIcon(self._preview_icon(row, 64))
            self.table.setItem(row, 0, preview_item)
            self.table.setItem(row, 1, QTableWidgetItem(candidate.title))
            self.table.setItem(row, 2, QTableWidgetItem(candidate.provider))
            self.table.setItem(
                row, 3, QTableWidgetItem(f"{candidate.width}x{candidate.height}")
            )
            source_item = QTableWidgetItem(candidate.source_url)
            source_item.setToolTip(candidate.source_url)
            self.table.setItem(row, 4, source_item)
            self.table.setRowHeight(row, 72)
        self.table.setColumnWidth(0, 84)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        options_row = QHBoxLayout()
        self.circular_ring_check = QCheckBox("Circular + ring style")
        self.circular_ring_check.setChecked(True)
        self.circular_ring_check.toggled.connect(self._refresh_preview_icons)
        options_row.addWidget(self.circular_ring_check)
        options_row.addStretch(1)
        self.local_btn = QPushButton("Use Local Image...")
        self.local_btn.clicked.connect(self._on_pick_local)
        options_row.addWidget(self.local_btn)
        layout.addLayout(options_row)

        layout.addWidget(QLabel("InfoTip (optional):"))
        self.info_tip_edit = QPlainTextEdit(self)
        self.info_tip_edit.setPlaceholderText("Optional folder tooltip text")
        self.info_tip_edit.setFixedHeight(90)
        layout.addWidget(self.info_tip_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_then_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self.table.viewport():
            if event.type() == QEvent.Type.MouseMove:
                index = self.table.indexAt(event.pos())
                if index.isValid() and index.column() == 0:
                    cell_rect = self.table.visualRect(index)
                    icon_hit = cell_rect.adjusted(0, 0, -(cell_rect.width() - 74), 0)
                    if icon_hit.contains(event.pos()):
                        self._show_hover_preview(
                            index.row(),
                            self.table.viewport().mapToGlobal(event.pos()),
                        )
                        return False
                self._hide_hover_preview()
            elif event.type() in (
                QEvent.Type.Leave,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            ):
                self._hide_hover_preview()
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._hide_hover_preview()
        super().closeEvent(event)

    def _preview_icon(self, row: int, size: int) -> QIcon:
        pix = self._preview_pixmap(row, size)
        if pix is None or pix.isNull():
            return QIcon()
        return QIcon(pix)

    def _preview_pixmap(self, row: int, size: int) -> QPixmap | None:
        if row < 0 or row >= len(self.candidates):
            return None
        circular_ring = (
            self.circular_ring_check.isChecked()
            if hasattr(self, "circular_ring_check")
            else True
        )
        cache_key = (row, size, circular_ring)
        cached = self._preview_pix_cache.get(cache_key)
        if cached is not None:
            return cached
        candidate = self.candidates[row]
        try:
            preview_png = self._preview_loader(candidate, circular_ring, size)
            pix = QPixmap()
            if not pix.loadFromData(preview_png):
                return None
            self._preview_pix_cache[cache_key] = pix
            return pix
        except Exception:
            return None

    def _refresh_preview_icons(self) -> None:
        self._hide_hover_preview()
        for row in range(len(self.candidates)):
            item = self.table.item(row, 0)
            if item is None:
                continue
            item.setIcon(self._preview_icon(row, 64))

    def _show_hover_preview(self, row: int, global_pos: QPoint) -> None:
        pix = self._preview_pixmap(row, 256)
        if pix is None or pix.isNull():
            self._hide_hover_preview()
            return
        if self._hover_row != row:
            self._hover_popup.setPixmap(pix)
            self._hover_popup.adjustSize()
            self._hover_row = row
        self._position_hover_popup(global_pos)
        self._hover_popup.show()

    def _position_hover_popup(self, global_pos: QPoint) -> None:
        popup_size = self._hover_popup.sizeHint()
        x = global_pos.x() + 20
        y = global_pos.y() + 20
        screen = QApplication.primaryScreen()
        if screen is not None:
            rect = screen.availableGeometry()
            x = min(max(rect.left(), x), max(rect.left(), rect.right() - popup_size.width()))
            y = min(max(rect.top(), y), max(rect.top(), rect.bottom() - popup_size.height()))
        self._hover_popup.move(x, y)

    def _hide_hover_preview(self) -> None:
        self._hover_popup.hide()
        self._hover_row = None

    def _on_pick_local(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*)",
        )
        if not selected:
            return
        self._local_image_path = selected
        self.table.clearSelection()
        QMessageBox.information(self, "Local Image Selected", selected)

    def _validate_then_accept(self) -> None:
        row = self.table.currentRow()
        if row < 0 and not self._local_image_path:
            QMessageBox.warning(
                self,
                "No Selection",
                "Select one candidate row or choose a local image.",
            )
            return
        self.accept()

    def result_payload(self) -> IconPickerResult:
        row = self.table.currentRow()
        candidate = self.candidates[row] if 0 <= row < len(self.candidates) else None
        return IconPickerResult(
            candidate=candidate,
            local_image_path=self._local_image_path,
            info_tip=self.info_tip_edit.toPlainText().strip(),
            circular_ring=self.circular_ring_check.isChecked(),
        )


class IconProviderSettingsDialog(QDialog):
    def __init__(
        self,
        initial: IconProviderSettingsResult,
        test_callback,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Icon Provider Settings")
        self._test_callback = test_callback

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Configure API keys/endpoints for icon sources."))

        self.steam_enabled = QCheckBox("Enable SteamGridDB")
        self.steam_enabled.setChecked(initial.steamgriddb_enabled)
        layout.addWidget(self.steam_enabled)
        self.steam_key = QLineEdit(initial.steamgriddb_api_key)
        self.steam_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.steam_key.setPlaceholderText("SteamGridDB API Key")
        layout.addWidget(self.steam_key)
        self.steam_base = QLineEdit(initial.steamgriddb_api_base)
        self.steam_base.setPlaceholderText("SteamGridDB API Base URL")
        layout.addWidget(self.steam_base)

        self.iconfinder_enabled = QCheckBox("Enable Iconfinder")
        self.iconfinder_enabled.setChecked(initial.iconfinder_enabled)
        layout.addWidget(self.iconfinder_enabled)
        self.iconfinder_key = QLineEdit(initial.iconfinder_api_key)
        self.iconfinder_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.iconfinder_key.setPlaceholderText("Iconfinder API Key")
        layout.addWidget(self.iconfinder_key)
        self.iconfinder_base = QLineEdit(initial.iconfinder_api_base)
        self.iconfinder_base.setPlaceholderText("Iconfinder API Base URL")
        layout.addWidget(self.iconfinder_base)

        actions = QHBoxLayout()
        self.test_btn = QPushButton("Test Credentials")
        self.test_btn.clicked.connect(self._on_test)
        actions.addWidget(self.test_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_test(self) -> None:
        try:
            msg = self._test_callback(self.result_payload())
        except Exception as exc:
            QMessageBox.warning(self, "Credentials Test", f"Test failed:\n{exc}")
            return
        QMessageBox.information(self, "Credentials Test", msg)

    def result_payload(self) -> IconProviderSettingsResult:
        return IconProviderSettingsResult(
            steamgriddb_enabled=self.steam_enabled.isChecked(),
            steamgriddb_api_key=self.steam_key.text().strip(),
            steamgriddb_api_base=self.steam_base.text().strip(),
            iconfinder_enabled=self.iconfinder_enabled.isChecked(),
            iconfinder_api_key=self.iconfinder_key.text().strip(),
            iconfinder_api_base=self.iconfinder_base.text().strip(),
        )


class DeleteGroupDialog(QDialog):
    def __init__(
        self,
        cleaned_name: str,
        rows: list[tuple[InventoryItem, str]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Deleting {cleaned_name}")
        self.cancel_all_requested = False
        self.rows = list(
            sorted(
                rows,
                key=lambda x: x[0].modified_at,
                reverse=True,
            )
        )

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Select versions to delete. The newest version is unselected by default."
            )
        )

        self.table = QTableWidget(len(self.rows), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Delete", "Full Name", "Modified", "Size", "Source"]
        )
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)

        for row_idx, (item, source_text) in enumerate(self.rows):
            check_item = QTableWidgetItem("")
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            # Newest is first row after sort(desc), default unchecked.
            check_item.setCheckState(
                Qt.CheckState.Unchecked if row_idx == 0 else Qt.CheckState.Checked
            )
            self.table.setItem(row_idx, 0, check_item)
            self.table.setItem(row_idx, 1, QTableWidgetItem(item.full_name))
            self.table.setItem(
                row_idx, 2, QTableWidgetItem(item.modified_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.table.setItem(row_idx, 3, QTableWidgetItem(str(item.size_bytes)))
            self.table.setItem(row_idx, 4, QTableWidgetItem(source_text))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        self.ok_btn = QPushButton("Confirm")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_all_btn = QPushButton("Cancel All")
        buttons.addButton(self.ok_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(self.cancel_all_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.ok_btn.clicked.connect(self._validate_then_accept)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_all_btn.clicked.connect(self._cancel_all)
        layout.addWidget(buttons)

        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def selected_for_delete(self) -> list[InventoryItem]:
        selected: list[InventoryItem] = []
        for row_idx, (item, _) in enumerate(self.rows):
            check_item = self.table.item(row_idx, 0)
            if check_item and check_item.checkState() == Qt.CheckState.Checked:
                selected.append(item)
        return selected

    def _validate_then_accept(self) -> None:
        selected = self.selected_for_delete()
        if not selected:
            answer = QMessageBox.question(
                self,
                "No Selection",
                "No versions are selected for deletion. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.accept()
            return
        if len(selected) == len(self.rows):
            answer = QMessageBox.warning(
                self,
                "Delete All Versions",
                "All versions are selected. Are you certain you want to delete all versions of this game?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def _cancel_all(self) -> None:
        self.cancel_all_requested = True
        self.reject()
