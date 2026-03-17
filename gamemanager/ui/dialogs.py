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

from gamemanager.models import MovePlanItem, RenamePlanItem, TagCandidate


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
        self.resize(850, 480)
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
        self.resize(950, 520)
        self.plan_items = plan_items

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Safe cleanup will rename non-conflicting root-level items on disk. "
                "Conflicts are flagged for manual rename."
            )
        )

        self.table = QTableWidget(len(plan_items), 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Proposed Name", "Destination", "Status"]
        )
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        for row, item in enumerate(plan_items):
            status = item.status
            if status == "ready":
                status_text = "Ready"
            elif status == "unchanged":
                status_text = "Unchanged"
            else:
                status_text = "Conflict - manual rename required"
            self.table.setItem(row, 0, QTableWidgetItem(str(item.src_path)))
            self.table.setItem(row, 1, QTableWidgetItem(item.proposed_name))
            self.table.setItem(row, 2, QTableWidgetItem(str(item.dst_path)))
            self.table.setItem(row, 3, QTableWidgetItem(status_text))
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

    def safe_items(self) -> list[RenamePlanItem]:
        return [item for item in self.plan_items if item.status == "ready"]


class MovePreviewDialog(QDialog):
    def __init__(self, plan_items: list[MovePlanItem], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Archive Move Preview")
        self.resize(1100, 560)
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

