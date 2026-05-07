from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from gamemanager.services.browser_downloads import detect_browser_download_dir
from .common import IconProviderSettingsResult, PerformanceSettingsResult
from .shared import bind_dialog_shortcut as _bind_dialog_shortcut


class PerformanceSettingsDialog(QDialog):
    def __init__(
        self,
        initial: PerformanceSettingsResult,
        cleanup_backups_callback: Callable[[], None] | None = None,
        parent: QDialog | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Performance Settings")
        self._cleanup_backups_callback = cleanup_backups_callback

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Tune scan/move responsiveness and cache behavior. "
                "Higher worker counts can improve throughput on fast storage."
            )
        )

        prewarm_row = QHBoxLayout()
        prewarm_row.addWidget(QLabel("Startup model preload:", self))
        self.prewarm_mode_combo = QComboBox(self)
        self.prewarm_mode_combo.addItem("Off", "off")
        self.prewarm_mode_combo.addItem("Minimal", "minimal")
        self.prewarm_mode_combo.addItem("Full", "full")
        mode = str(initial.startup_prewarm_mode or "minimal").strip().casefold()
        idx = self.prewarm_mode_combo.findData(mode if mode in {"off", "minimal", "full"} else "minimal")
        if idx >= 0:
            self.prewarm_mode_combo.setCurrentIndex(idx)
        prewarm_row.addWidget(self.prewarm_mode_combo)
        prewarm_row.addStretch(1)
        layout.addLayout(prewarm_row)

        workers_row = QHBoxLayout()
        workers_row.addWidget(QLabel("Folder-size workers (0 = auto):", self))
        self.workers_spin = QSpinBox(self)
        self.workers_spin.setRange(0, 64)
        self.workers_spin.setValue(max(0, min(64, int(initial.scan_size_workers))))
        workers_row.addWidget(self.workers_spin)
        workers_row.addStretch(1)
        layout.addLayout(workers_row)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Progress update interval (ms):", self))
        self.interval_spin = QSpinBox(self)
        self.interval_spin.setRange(10, 500)
        self.interval_spin.setValue(max(10, min(500, int(initial.progress_interval_ms))))
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)

        cache_row = QHBoxLayout()
        self.cache_enabled = QCheckBox("Enable directory-size cache", self)
        self.cache_enabled.setChecked(bool(initial.dir_cache_enabled))
        cache_row.addWidget(self.cache_enabled)
        cache_row.addStretch(1)
        layout.addLayout(cache_row)

        rebuild_backup_row = QHBoxLayout()
        self.rebuild_backup_enabled = QCheckBox(
            "Create backups during icon rebuild",
            self,
        )
        self.rebuild_backup_enabled.setChecked(bool(initial.icon_rebuild_create_backups))
        rebuild_backup_row.addWidget(self.rebuild_backup_enabled)
        rebuild_backup_row.addStretch(1)
        layout.addLayout(rebuild_backup_row)

        rebuild_mode_row = QHBoxLayout()
        rebuild_mode_row.addWidget(QLabel("Icon rebuild mode:", self))
        self.rebuild_mode_combo = QComboBox(self)
        self.rebuild_mode_combo.addItem("Guided (Preview + Tuning)", "guided")
        self.rebuild_mode_combo.addItem("Automatic (Use Saved Defaults)", "automatic")
        mode = str(initial.icon_rebuild_mode or "guided").strip().casefold()
        idx = self.rebuild_mode_combo.findData(mode if mode in {"guided", "automatic"} else "guided")
        if idx >= 0:
            self.rebuild_mode_combo.setCurrentIndex(idx)
        rebuild_mode_row.addWidget(self.rebuild_mode_combo)
        rebuild_mode_row.addStretch(1)
        layout.addLayout(rebuild_mode_row)

        popup_row = QHBoxLayout()
        self.success_popups_enabled = QCheckBox(
            "Show success confirmation popups",
            self,
        )
        self.success_popups_enabled.setChecked(bool(initial.success_popups_enabled))
        popup_row.addWidget(self.success_popups_enabled)
        popup_row.addStretch(1)
        layout.addLayout(popup_row)

        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("Cache max entries:", self))
        self.cache_max_spin = QSpinBox(self)
        self.cache_max_spin.setRange(1_000, 2_000_000)
        self.cache_max_spin.setSingleStep(10_000)
        self.cache_max_spin.setValue(
            max(1_000, min(2_000_000, int(initial.dir_cache_max_entries)))
        )
        max_row.addWidget(self.cache_max_spin)
        max_row.addStretch(1)
        layout.addLayout(max_row)

        web_mode_row = QHBoxLayout()
        web_mode_row.addWidget(QLabel("Web capture folder:", self))
        self.web_capture_mode_combo = QComboBox(self)
        self.web_capture_mode_combo.addItem("Auto-detect", "auto")
        self.web_capture_mode_combo.addItem("Manual", "manual")
        mode = str(initial.web_capture_download_mode or "auto").strip().casefold()
        mode_idx = self.web_capture_mode_combo.findData(
            mode if mode in {"auto", "manual"} else "auto"
        )
        if mode_idx >= 0:
            self.web_capture_mode_combo.setCurrentIndex(mode_idx)
        web_mode_row.addWidget(self.web_capture_mode_combo)
        web_mode_row.addStretch(1)
        layout.addLayout(web_mode_row)

        web_dir_row = QHBoxLayout()
        web_dir_row.addWidget(QLabel("Downloads path:", self))
        self.web_capture_dir_edit = QLineEdit(
            str(initial.web_capture_download_dir or "").strip(),
            self,
        )
        self.web_capture_dir_edit.setPlaceholderText(
            "Used when mode is Manual"
        )
        web_dir_row.addWidget(self.web_capture_dir_edit, 1)
        self.web_capture_dir_browse_btn = QPushButton("Browse...", self)
        self.web_capture_dir_browse_btn.clicked.connect(self._on_browse_web_capture_dir)
        web_dir_row.addWidget(self.web_capture_dir_browse_btn)
        self.web_capture_dir_detect_btn = QPushButton("Detect", self)
        self.web_capture_dir_detect_btn.clicked.connect(
            self._on_detect_web_capture_dir
        )
        web_dir_row.addWidget(self.web_capture_dir_detect_btn)
        layout.addLayout(web_dir_row)

        self.web_capture_mode_combo.currentIndexChanged.connect(
            self._sync_web_capture_dir_controls
        )
        self._sync_web_capture_dir_controls()

        maintenance_row = QHBoxLayout()
        self.clean_backups_btn = QPushButton("Clean Backup Icons...", self)
        self.clean_backups_btn.setToolTip("Delete all *.gm_backup_*.ico files under configured roots")
        self.clean_backups_btn.clicked.connect(self._on_clean_backups_clicked)
        self.clean_backups_btn.setEnabled(self._cleanup_backups_callback is not None)
        maintenance_row.addWidget(self.clean_backups_btn)
        maintenance_row.addStretch(1)
        layout.addLayout(maintenance_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setToolTip("Save settings\nShortcut: Ctrl+Enter")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setToolTip("Cancel\nShortcut: Esc")
        layout.addWidget(buttons)
        _bind_dialog_shortcut(self, "Ctrl+Return", self.accept)
        _bind_dialog_shortcut(self, "Ctrl+Enter", self.accept)
        _bind_dialog_shortcut(self, "F1", self._show_shortcuts)

    def result_payload(self) -> PerformanceSettingsResult:
        return PerformanceSettingsResult(
            scan_size_workers=int(self.workers_spin.value()),
            progress_interval_ms=int(self.interval_spin.value()),
            dir_cache_enabled=self.cache_enabled.isChecked(),
            dir_cache_max_entries=int(self.cache_max_spin.value()),
            startup_prewarm_mode=str(self.prewarm_mode_combo.currentData() or "minimal"),
            success_popups_enabled=self.success_popups_enabled.isChecked(),
            web_capture_download_mode=str(
                self.web_capture_mode_combo.currentData() or "auto"
            ),
            web_capture_download_dir=self.web_capture_dir_edit.text().strip(),
            icon_rebuild_create_backups=self.rebuild_backup_enabled.isChecked(),
            icon_rebuild_mode=str(self.rebuild_mode_combo.currentData() or "guided"),
        )

    def _on_clean_backups_clicked(self) -> None:
        if self._cleanup_backups_callback is None:
            QMessageBox.information(
                self,
                "Clean Backup Icons",
                "Cleanup is not available in this context.",
            )
            return
        try:
            self._cleanup_backups_callback()
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Clean Backup Icons",
                f"Cleanup failed:\n{exc}",
            )

    def _on_browse_web_capture_dir(self) -> None:
        current = self.web_capture_dir_edit.text().strip()
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Browser Downloads Folder",
            current or str(Path.home() / "Downloads"),
        )
        if not selected:
            return
        self.web_capture_dir_edit.setText(selected)
        idx = self.web_capture_mode_combo.findData("manual")
        if idx >= 0:
            self.web_capture_mode_combo.setCurrentIndex(idx)

    def _on_detect_web_capture_dir(self) -> None:
        detected = detect_browser_download_dir()
        self.web_capture_dir_edit.setText(str(detected.download_dir))
        idx = self.web_capture_mode_combo.findData("auto")
        if idx >= 0:
            self.web_capture_mode_combo.setCurrentIndex(idx)
        QMessageBox.information(
            self,
            "Web Capture Detection",
            (
                f"Detected: {detected.browser_label}\n"
                f"Folder: {detected.download_dir}\n"
                f"Source: {detected.source}"
            ),
        )

    def _sync_web_capture_dir_controls(self) -> None:
        mode = str(self.web_capture_mode_combo.currentData() or "auto")
        manual = mode == "manual"
        self.web_capture_dir_edit.setEnabled(manual)
        self.web_capture_dir_browse_btn.setEnabled(manual)
        self.web_capture_dir_detect_btn.setEnabled(not manual)

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Performance Shortcuts",
            "\n".join(
                [
                    "Ctrl+Enter - Save settings",
                    "Esc - Cancel",
                    "F1 - Show shortcuts",
                ]
            ),
        )


class IconProviderSettingsDialog(QDialog):
    def __init__(
        self,
        initial: IconProviderSettingsResult,
        test_callback,
        parent: QDialog | None = None,
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

        self.igdb_enabled = QCheckBox("Enable IGDB")
        self.igdb_enabled.setChecked(initial.igdb_enabled)
        layout.addWidget(self.igdb_enabled)
        self.igdb_client_id = QLineEdit(initial.igdb_client_id)
        self.igdb_client_id.setPlaceholderText("IGDB Client ID")
        layout.addWidget(self.igdb_client_id)
        self.igdb_client_secret = QLineEdit(initial.igdb_client_secret)
        self.igdb_client_secret.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.igdb_client_secret.setPlaceholderText("IGDB Client Secret")
        layout.addWidget(self.igdb_client_secret)
        self.igdb_base = QLineEdit(initial.igdb_api_base)
        self.igdb_base.setPlaceholderText("IGDB API Base URL")
        layout.addWidget(self.igdb_base)

        actions = QHBoxLayout()
        self.test_btn = QPushButton("Test Credentials")
        self.test_btn.setToolTip("Test provider credentials\nShortcut: Alt+T")
        self.test_btn.clicked.connect(self._on_test)
        actions.addWidget(self.test_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setToolTip("Save provider settings\nShortcut: Ctrl+Enter")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setToolTip("Cancel\nShortcut: Esc")
        layout.addWidget(buttons)
        _bind_dialog_shortcut(self, "Alt+T", self._on_test)
        _bind_dialog_shortcut(self, "Ctrl+Return", self.accept)
        _bind_dialog_shortcut(self, "Ctrl+Enter", self.accept)
        _bind_dialog_shortcut(self, "F1", self._show_shortcuts)

    def _on_test(self) -> None:
        try:
            msg = self._test_callback(self.result_payload())
        except Exception as exc:
            QMessageBox.warning(self, "Credentials Test", f"Test failed:\n{exc}")
            return
        QMessageBox.information(self, "Credentials Test", msg)

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Icon Provider Shortcuts",
            "\n".join(
                [
                    "Alt+T - Test credentials",
                    "Ctrl+Enter - Save settings",
                    "Esc - Cancel",
                    "F1 - Show shortcuts",
                ]
            ),
        )

    def result_payload(self) -> IconProviderSettingsResult:
        return IconProviderSettingsResult(
            steamgriddb_enabled=self.steam_enabled.isChecked(),
            steamgriddb_api_key=self.steam_key.text().strip(),
            steamgriddb_api_base=self.steam_base.text().strip(),
            igdb_enabled=self.igdb_enabled.isChecked(),
            igdb_client_id=self.igdb_client_id.text().strip(),
            igdb_client_secret=self.igdb_client_secret.text().strip(),
            igdb_api_base=self.igdb_base.text().strip(),
        )


__all__ = [
    "IconProviderSettingsDialog",
    "IconProviderSettingsResult",
    "PerformanceSettingsDialog",
    "PerformanceSettingsResult",
]
