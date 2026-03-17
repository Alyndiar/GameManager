from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gamemanager.models import InventoryItem, MovePlanItem, RenamePlanItem, TagCandidate


@dataclass(slots=True)
class TagReviewResult:
    decisions: dict[str, str]
    display_map: dict[str, str]


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
