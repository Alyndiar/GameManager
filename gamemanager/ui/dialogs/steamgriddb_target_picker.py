from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from gamemanager.models import SgdbGameCandidate


class SgdbTargetPickerDialog(QDialog):
    def __init__(
        self,
        *,
        folder_name: str,
        candidates: list[SgdbGameCandidate],
        drift_reasons: list[str] | None = None,
        icon_path: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Select SteamGridDB Target")
        self._candidates = list(candidates or [])
        self._selected: SgdbGameCandidate | None = None

        layout = QVBoxLayout(self)
        header = QLabel(
            (
                "Pick the exact SteamGridDB game for this folder before upload.\n"
                f"Folder: {folder_name}"
            ),
            self,
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        preview = QLabel(self)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setFixedSize(272, 272)
        preview.setStyleSheet("QLabel { background: #101010; border: 1px solid #303030; }")
        icon_token = str(icon_path or "").strip()
        pixmap = QIcon(icon_token).pixmap(256, 256) if icon_token else QPixmap()
        if pixmap.isNull() and icon_token:
            fallback = QPixmap(icon_token)
            if not fallback.isNull():
                pixmap = fallback.scaled(
                    256,
                    256,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        if pixmap.isNull():
            preview.setText("No preview")
        else:
            preview.setPixmap(pixmap)
        layout.addWidget(preview, 0, Qt.AlignmentFlag.AlignLeft)

        if drift_reasons:
            drift_label = QLabel(
                "Saved binding requires reconfirmation:\n- " + "\n- ".join(drift_reasons),
                self,
            )
            drift_label.setWordWrap(True)
            layout.addWidget(drift_label)

        self.table = QTableWidget(len(self._candidates), 4, self)
        self.table.setHorizontalHeaderLabels(["Game ID", "Title", "Confidence", "Evidence"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, candidate in enumerate(self._candidates):
            self.table.setItem(row, 0, QTableWidgetItem(str(int(candidate.game_id))))
            self.table.setItem(row, 1, QTableWidgetItem(candidate.title))
            self.table.setItem(row, 2, QTableWidgetItem(f"{float(candidate.confidence):.2f}"))
            evidence = "; ".join(candidate.evidence[:3])
            evidence_item = QTableWidgetItem(evidence)
            evidence_item.setToolTip("\n".join(candidate.evidence))
            self.table.setItem(row, 3, evidence_item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        if self._candidates:
            self.table.selectRow(0)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("Manual SGDB Game ID:", self))
        self.manual_id_edit = QLineEdit(self)
        self.manual_id_edit.setPlaceholderText("Optional numeric ID")
        manual_row.addWidget(self.manual_id_edit, 1)
        layout.addLayout(manual_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText("Select")
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("Skip")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(900, 720)

    def _on_accept(self) -> None:
        manual_token = self.manual_id_edit.text().strip()
        if manual_token:
            if not manual_token.isdigit():
                QMessageBox.warning(self, "Invalid Game ID", "Manual game ID must be numeric.")
                return
            self._selected = SgdbGameCandidate(
                game_id=int(manual_token),
                title=f"SGDB Game {int(manual_token)}",
                confidence=1.0,
                evidence=[f"Manual SGDB ID {int(manual_token)}"],
                steam_appid=None,
            )
            self.accept()
            return

        row = self.table.currentRow()
        if row < 0 or row >= len(self._candidates):
            QMessageBox.warning(self, "No Target Selected", "Select a game target or enter a manual SGDB game ID.")
            return
        self._selected = self._candidates[row]
        self.accept()

    def selected_candidate(self) -> SgdbGameCandidate | None:
        return self._selected
