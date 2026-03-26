from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gamemanager.runtime import (
    DEFAULT_ICONMAKER_GAMEMANAGER_MUTEX,
    AppInstanceLock,
    show_already_running_message,
)
from gamemanager.services.persistent_workers import (
    ensure_persistent_icon_workers_async,
    shutdown_persistent_icon_workers,
)
from iconmaker_gui.dialogs import (
    IconConverterDialog,
    TemplatePrepDialog,
    TemplateTransparencyDialog,
)


class IconMakerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Icon Maker")
        self._converter_dialog: IconConverterDialog | None = None
        self._template_prep_dialog: TemplatePrepDialog | None = None
        self._template_transparency_dialog: TemplateTransparencyDialog | None = None

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Icon Construction Toolkit", self)
        title.setStyleSheet("QLabel { font-size: 18px; font-weight: 600; }")
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                "Standalone entrypoint for icon construction and template tooling.\n"
                "Uses the same dialog stack as GameManager for feature parity.",
                self,
            )
        )

        buttons_row = QHBoxLayout()
        self.converter_btn = QPushButton("Image to Icon Converter...", self)
        self.converter_btn.clicked.connect(self._on_open_converter)
        buttons_row.addWidget(self.converter_btn)

        self.template_prep_btn = QPushButton("Template Batch Prep...", self)
        self.template_prep_btn.clicked.connect(self._on_open_template_prep)
        buttons_row.addWidget(self.template_prep_btn)

        self.template_alpha_btn = QPushButton("Template Transparency...", self)
        self.template_alpha_btn.clicked.connect(self._on_open_template_transparency)
        buttons_row.addWidget(self.template_alpha_btn)

        layout.addLayout(buttons_row)
        layout.addStretch(1)

        self.runtime_status = QLabel("", self)
        layout.addWidget(self.runtime_status)
        self._refresh_runtime_status()

    def _refresh_runtime_status(self) -> None:
        from gamemanager.services.persistent_workers import persistent_icon_workers_status

        status = persistent_icon_workers_status()
        self.runtime_status.setText(
            f"Persistent workers: {int(status.get('node_count', 0))} "
            f"| running={bool(status.get('running', False))}"
        )

    def _on_open_converter(self) -> None:
        if self._converter_dialog is not None:
            self._converter_dialog.show()
            self._converter_dialog.raise_()
            self._converter_dialog.activateWindow()
            return
        dialog = IconConverterDialog(parent=self)
        dialog.finished.connect(self._on_converter_closed)
        self._converter_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_converter_closed(self, _result: int) -> None:
        self._converter_dialog = None

    def _on_open_template_prep(self) -> None:
        if self._template_prep_dialog is not None:
            self._template_prep_dialog.show()
            self._template_prep_dialog.raise_()
            self._template_prep_dialog.activateWindow()
            return
        dialog = TemplatePrepDialog(parent=self)
        dialog.finished.connect(self._on_template_prep_closed)
        self._template_prep_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_template_prep_closed(self, _result: int) -> None:
        self._template_prep_dialog = None

    def _on_open_template_transparency(self) -> None:
        if self._template_transparency_dialog is not None:
            self._template_transparency_dialog.show()
            self._template_transparency_dialog.raise_()
            self._template_transparency_dialog.activateWindow()
            return
        dialog = TemplateTransparencyDialog(parent=self)
        dialog.finished.connect(self._on_template_transparency_closed)
        self._template_transparency_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_template_transparency_closed(self, _result: int) -> None:
        self._template_transparency_dialog = None


def run() -> int:
    instance_lock = AppInstanceLock(DEFAULT_ICONMAKER_GAMEMANAGER_MUTEX)
    if not instance_lock.acquire():
        show_already_running_message(
            current_app_name="IconMaker",
            other_app_name="GameManager or IconMaker",
        )
        return 1
    app = QApplication(sys.argv)
    app.aboutToQuit.connect(shutdown_persistent_icon_workers)
    window = IconMakerWindow()
    screen = app.primaryScreen()
    if screen is not None:
        window.setGeometry(screen.availableGeometry())
    window.showMaximized()
    from PySide6.QtCore import QTimer

    QTimer.singleShot(1200, lambda: ensure_persistent_icon_workers_async(worker_count=2))
    try:
        return app.exec()
    finally:
        shutdown_persistent_icon_workers()
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(run())
