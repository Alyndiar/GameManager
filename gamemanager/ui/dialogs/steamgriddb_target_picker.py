from __future__ import annotations

from collections.abc import Callable
import os
import subprocess
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
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
)

from gamemanager.models import SgdbGameCandidate
from gamemanager.services.storefronts.priority import normalize_store_name, sort_stores
from gamemanager.services.storefronts.store_urls import store_game_url


class SgdbTargetPickerDialog(QDialog):
    _DEFAULT_MANUAL_STORE_ORDER: tuple[str, ...] = (
        "Steam",
        "EGS",
        "GOG",
        "Itch.io",
        "Humble",
        "Ubisoft",
        "Battle.net",
        "Amazon Games",
    )

    def __init__(
        self,
        *,
        folder_name: str,
        folder_path: str = "",
        candidates: list[SgdbGameCandidate],
        drift_reasons: list[str] | None = None,
        icon_path: str = "",
        manual_store_options: list[str] | None = None,
        owned_store_targets: list[dict[str, str]] | None = None,
        manual_id_resolver: Callable[[str, str], SgdbGameCandidate] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Select SteamGridDB Target")
        self._candidates = list(candidates or [])
        self._selected: SgdbGameCandidate | None = None
        self._folder_path = str(folder_path or "").strip()
        self._manual_id_resolver = manual_id_resolver
        self._owned_store_targets = list(owned_store_targets or [])
        self.cancel_all_requested = False
        self.owned_store_combo: QComboBox | None = None
        self.open_owned_store_btn: QPushButton | None = None

        layout = QVBoxLayout(self)
        header = QLabel(
            (
                "Pick the exact game target for this folder.\n"
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
                "Target requires reconfirmation:\n- " + "\n- ".join(drift_reasons),
                self,
            )
            drift_label.setWordWrap(True)
            layout.addWidget(drift_label)

        self.table = QTableWidget(len(self._candidates), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["SGDB ID", "Steam AppID", "Title", "Confidence", "Evidence"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, candidate in enumerate(self._candidates):
            self.table.setItem(row, 0, QTableWidgetItem(str(int(candidate.game_id))))
            self.table.setItem(row, 1, QTableWidgetItem(str(candidate.steam_appid or "")))
            self.table.setItem(row, 2, QTableWidgetItem(candidate.title))
            self.table.setItem(row, 3, QTableWidgetItem(f"{float(candidate.confidence):.2f}"))
            evidence = "; ".join(candidate.evidence[:3])
            evidence_item = QTableWidgetItem(evidence)
            evidence_item.setToolTip("\n".join(candidate.evidence))
            self.table.setItem(row, 4, evidence_item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self.table.itemSelectionChanged.connect(self._update_link_buttons)
        layout.addWidget(self.table)
        if self._candidates:
            self.table.selectRow(0)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("Manual ID:", self))
        self.manual_source_combo = QComboBox(self)
        for label, value in self._manual_sources(manual_store_options):
            self.manual_source_combo.addItem(label, value)
        manual_row.addWidget(self.manual_source_combo, 0)
        self.manual_id_edit = QLineEdit(self)
        self.manual_id_edit.setPlaceholderText("Enter ID for selected source")
        manual_row.addWidget(self.manual_id_edit, 1)
        layout.addLayout(manual_row)

        utility_row = QHBoxLayout()
        self.open_sgdb_btn = QPushButton("Open SGDB Page", self)
        self.open_sgdb_btn.clicked.connect(self._on_open_selected_sgdb_page)
        utility_row.addWidget(self.open_sgdb_btn)
        self.open_store_btn = QPushButton("Open Store Page", self)
        self.open_store_btn.clicked.connect(self._on_open_selected_store_page)
        utility_row.addWidget(self.open_store_btn)
        self.open_game_btn = QPushButton("Open Game Folder", self)
        self.open_game_btn.setEnabled(
            bool(self._folder_path and os.path.isdir(self._folder_path))
        )
        self.open_game_btn.clicked.connect(self._on_open_game_folder)
        utility_row.addWidget(self.open_game_btn)
        utility_row.addStretch(1)
        layout.addLayout(utility_row)

        if self._owned_store_targets:
            store_row = QHBoxLayout()
            store_row.addWidget(QLabel("Owned Store ID:", self))
            self.owned_store_combo = QComboBox(self)
            for row in self._owned_store_targets:
                store_name = str(row.get("store_name") or "").strip()
                store_id = str(row.get("store_id") or "").strip()
                label = f"{store_name}: {store_id}" if store_id else store_name
                self.owned_store_combo.addItem(label, str(row.get("url") or "").strip())
            store_row.addWidget(self.owned_store_combo, 1)
            self.open_owned_store_btn = QPushButton("Open Selected Store Page", self)
            self.open_owned_store_btn.clicked.connect(self._on_open_owned_store_page)
            store_row.addWidget(self.open_owned_store_btn)
            self.owned_store_combo.currentIndexChanged.connect(self._update_link_buttons)
            layout.addLayout(store_row)

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
        self.cancel_all_btn = buttons.addButton(
            "Cancel All",
            QDialogButtonBox.ButtonRole.DestructiveRole,
        )
        self.cancel_all_btn.clicked.connect(self._on_cancel_all)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_link_buttons()
        self.resize(980, 760)

    @classmethod
    def _manual_sources(
        cls,
        manual_store_options: list[str] | None,
    ) -> list[tuple[str, str]]:
        ordered: list[str] = []
        seen: set[str] = set()
        for raw in list(manual_store_options or []) + list(cls._DEFAULT_MANUAL_STORE_ORDER):
            canonical = normalize_store_name(raw)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            ordered.append(canonical)
        out = [("SGDB Game ID", "SGDB")]
        for store in ordered:
            out.append((f"{store} ID", store))
        return out

    def _selected_candidate(self) -> SgdbGameCandidate | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._candidates):
            return None
        return self._candidates[row]

    @staticmethod
    def _sgdb_url_for_candidate(candidate: SgdbGameCandidate) -> str:
        if int(candidate.game_id) <= 0:
            return ""
        return f"https://www.steamgriddb.com/game/{int(candidate.game_id)}"

    @staticmethod
    def _steam_url_for_candidate(candidate: SgdbGameCandidate) -> str:
        appid = str(candidate.steam_appid or "").strip()
        if appid.isdigit():
            return f"https://store.steampowered.com/app/{appid}/"
        identity_store = normalize_store_name(str(candidate.identity_store or "").strip())
        identity_store_id = str(candidate.identity_store_id or "").strip()
        if not identity_store:
            store_ids = dict(getattr(candidate, "store_ids", {}) or {})
            for store in sort_stores(list(store_ids.keys())):
                token = str(store_ids.get(store, "")).strip()
                if not token:
                    continue
                return store_game_url(
                    store,
                    store_game_id=token,
                    title=str(candidate.title or "").strip(),
                )
            return ""
        return store_game_url(
            identity_store,
            store_game_id=identity_store_id,
            title=str(candidate.title or "").strip(),
        )

    @staticmethod
    def _open_url(url: str) -> bool:
        token = str(url or "").strip()
        if not token:
            return False
        try:
            webbrowser.open(token, new=2)
            return True
        except Exception:
            return False

    def _update_link_buttons(self) -> None:
        candidate = self._selected_candidate()
        if hasattr(self, "open_sgdb_btn"):
            self.open_sgdb_btn.setEnabled(
                candidate is not None and bool(self._sgdb_url_for_candidate(candidate))
            )
        if hasattr(self, "open_store_btn"):
            self.open_store_btn.setEnabled(
                candidate is not None and bool(self._steam_url_for_candidate(candidate))
            )
        if self.open_owned_store_btn is not None and self.owned_store_combo is not None:
            self.open_owned_store_btn.setEnabled(bool(str(self.owned_store_combo.currentData() or "").strip()))

    def _on_open_selected_sgdb_page(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            return
        url = self._sgdb_url_for_candidate(candidate)
        if not self._open_url(url):
            QMessageBox.warning(self, "Open SGDB Page", f"Could not open:\n{url}")

    def _on_open_selected_store_page(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            return
        url = self._steam_url_for_candidate(candidate)
        if not self._open_url(url):
            QMessageBox.warning(self, "Open Store Page", f"Could not open:\n{url}")

    def _on_open_owned_store_page(self) -> None:
        if self.owned_store_combo is None:
            return
        url = str(self.owned_store_combo.currentData() or "").strip()
        if not self._open_url(url):
            QMessageBox.warning(self, "Open Store Page", f"Could not open:\n{url}")

    def _on_table_cell_double_clicked(self, row: int, column: int) -> None:
        if row < 0 or row >= len(self._candidates):
            return
        self.table.selectRow(row)
        if column == 0:
            self._on_open_selected_sgdb_page()
            return
        if column == 1:
            self._on_open_selected_store_page()

    def _on_open_game_folder(self) -> None:
        path = os.path.normpath(self._folder_path)
        if not path:
            return
        try:
            subprocess.Popen(["explorer", path])
        except OSError as exc:
            QMessageBox.warning(self, "Open Game Folder", f"Could not open:\n{path}\n\n{exc}")

    def _on_cancel_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "Cancel All",
            "Cancel the remaining items in this flow?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.cancel_all_requested = True
        self.reject()

    def _on_accept(self) -> None:
        manual_token = self.manual_id_edit.text().strip()
        if manual_token:
            source = str(self.manual_source_combo.currentData() or "SGDB").strip()
            try:
                if self._manual_id_resolver is not None:
                    self._selected = self._manual_id_resolver(source, manual_token)
                elif source == "SGDB":
                    if not manual_token.isdigit():
                        raise ValueError("SGDB Game ID must be numeric.")
                    self._selected = SgdbGameCandidate(
                        game_id=int(manual_token),
                        title=f"SGDB Game {int(manual_token)}",
                        confidence=1.0,
                        evidence=[f"Manual SGDB ID {int(manual_token)}"],
                        steam_appid=None,
                    )
                else:
                    raise ValueError(f"Manual resolver for {source} IDs is not configured.")
            except Exception as exc:
                QMessageBox.warning(self, "Manual ID Lookup Failed", str(exc))
                return
            if self._selected is not None and source != "SGDB":
                self._selected.identity_store = normalize_store_name(source)
                self._selected.identity_store_id = manual_token
                self._selected.store_ids = dict(self._selected.store_ids or {})
                self._selected.store_ids[self._selected.identity_store] = manual_token
            self.accept()
            return

        row = self.table.currentRow()
        if row < 0 or row >= len(self._candidates):
            QMessageBox.warning(
                self,
                "No Target Selected",
                "Select a game target or enter a manual store/SGDB ID.",
            )
            return
        self._selected = self._candidates[row]
        self.accept()

    def selected_candidate(self) -> SgdbGameCandidate | None:
        return self._selected
