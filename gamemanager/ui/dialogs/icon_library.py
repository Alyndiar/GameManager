from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import quote_plus
import webbrowser

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
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
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gamemanager.models import IconCandidate
from gamemanager.services.background_removal import (
    BACKGROUND_REMOVAL_OPTIONS,
    DEFAULT_BG_REMOVAL_PARAMS,
    normalize_background_removal_engine,
    normalize_background_removal_params,
)
from gamemanager.services.browser_downloads import (
    BrowserDownloadDetection,
    default_downloads_dir,
    detect_browser_download_dir,
)
from gamemanager.services.icon_pipeline import (
    BACKGROUND_FILL_MODE_OPTIONS,
    TEXT_EXTRACTION_METHOD_OPTIONS,
    border_shader_to_dict,
    build_preview_png,
    icon_style_options,
    normalize_background_fill_params,
    normalize_background_fill_mode,
    normalize_border_shader_config,
    normalize_icon_style,
    normalize_text_extraction_method,
    text_preserve_to_dict,
)
from gamemanager.services.image_prep import SUPPORTED_IMAGE_EXTENSIONS
from gamemanager.services.paths import project_data_dir
from gamemanager.ui.alpha_preview import composite_on_checkerboard
from .common import IconPickerResult
from .icon_construction import BorderShaderDialog, IconFramingDialog
from .shared import (
    bind_dialog_shortcut as _bind_dialog_shortcut,
    icon_style_gallery_entries as _icon_style_gallery_entries,
    normalize_image_bytes_for_canvas as _normalize_image_bytes_for_canvas,
)
from .template_management import TemplateGalleryDialog


SGDB_RESOURCE_OPTIONS: list[tuple[str, str]] = [
    ("Icons", "icons"),
    ("Logos", "logos"),
    ("Heroes", "heroes"),
    ("Grids", "grids"),
]


def _shader_swatch_css(rgb: tuple[int, int, int]) -> str:
    red, green, blue = rgb
    return (
        "QPushButton {"
        f" background-color: rgb({red}, {green}, {blue});"
        " border: 1px solid #555;"
        " min-width: 28px;"
        " min-height: 18px;"
        " }"
    )


def _is_supported_image_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def _google_image_search_url(query: str) -> str:
    if not query.strip():
        query = "game icon png transparent"
    return f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}"


def _web_capture_session_root() -> Path:
    legacy = project_data_dir() / "web_capture_sessions"
    if legacy.exists() and legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
    root = project_data_dir() / "web_capture_session"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _empty_directory(path: Path) -> None:
    if not path.exists():
        return
    for entry in list(path.iterdir()):
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _web_capture_session_dir() -> Path:
    return _web_capture_session_root()


def _cleanup_web_capture_session() -> None:
    _empty_directory(_web_capture_session_root())


def _bring_window_to_front(widget: QWidget) -> None:
    if widget.isMinimized():
        widget.showNormal()
    widget.show()
    widget.raise_()
    widget.activateWindow()
    window_handle = widget.windowHandle()
    if window_handle is not None:
        try:
            window_handle.requestActivate()
        except Exception:
            pass
    if os.name != "nt":
        return
    # Best-effort Windows foreground activation fallback when Qt focus requests are blocked.
    try:
        import ctypes

        hwnd = int(widget.winId())
        if hwnd <= 0:
            return
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        # Temporary topmost toggle helps force z-order refresh on some shells.
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
    except Exception:
        return


class ExternalDownloadWatcher:
    def __init__(
        self,
        downloads_dir: Path,
        poll_seconds: float = 1.0,
        on_detect: Callable[[Path], str | None] | None = None,
    ):
        self._downloads_dir = downloads_dir
        self._poll_seconds = max(0.25, poll_seconds)
        self._on_detect = on_detect
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._baseline: dict[str, tuple[int, int]] = {}
        self._captured_paths: set[str] = set()
        self._processed_sources: set[str] = set()
        self._new_paths: list[str] = []
        self._start_ts = 0.0
        self._end_ts: float | None = None

    @staticmethod
    def _is_image(path: Path) -> bool:
        return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        if not self._downloads_dir.exists():
            return snapshot
        try:
            with os.scandir(self._downloads_dir) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    path = Path(entry.path)
                    if not self._is_image(path):
                        continue
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    snapshot[str(path.resolve())] = (
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                    )
        except OSError:
            return snapshot
        return snapshot

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._baseline = self._snapshot()
        self._captured_paths.clear()
        self._processed_sources.clear()
        self._new_paths.clear()
        self._start_ts = time.time()
        self._end_ts = None
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _capture_from_snapshot(
        self,
        snapshot: dict[str, tuple[int, int]],
        *,
        final_pass: bool = False,
    ) -> None:
        end_ts = self._end_ts
        for path_str, payload in snapshot.items():
            size, mtime_ns = payload
            baseline_payload = self._baseline.get(path_str)
            if baseline_payload == (size, mtime_ns):
                continue
            if path_str in self._processed_sources:
                continue
            mtime_s = mtime_ns / 1_000_000_000
            if mtime_s + 2.0 < self._start_ts:
                continue
            if end_ts is not None and mtime_s > end_ts + 2.0:
                continue
            if not final_pass and (time.time() - mtime_s) < 1.0:
                continue
            source_path = Path(path_str)
            if not source_path.exists():
                continue
            captured_path = path_str
            if self._on_detect is not None:
                captured_path = self._on_detect(source_path) or ""
                if not captured_path:
                    continue
            with self._lock:
                self._captured_paths.add(captured_path)
                self._processed_sources.add(path_str)
                self._new_paths.append(captured_path)

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            snap = self._snapshot()
            self._capture_from_snapshot(snap)

    def stop(self) -> list[str]:
        self._end_ts = time.time()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        final_snapshot = self._snapshot()
        self._capture_from_snapshot(final_snapshot, final_pass=True)
        with self._lock:
            captured = [p for p in sorted(self._captured_paths) if Path(p).exists()]
        return captured

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pop_new_paths(self) -> list[str]:
        with self._lock:
            out = list(self._new_paths)
            self._new_paths.clear()
        return [path for path in out if Path(path).exists()]


class WebDownloadCaptureDialog(QDialog):
    def __init__(
        self,
        query: str,
        parent: QWidget | None = None,
        selection_callback: Callable[[str], None] | None = None,
        initial_download_dir: str | None = None,
        download_dir_saver: Callable[[str], None] | None = None,
        initial_download_mode: str = "auto",
        download_dir_mode_saver: Callable[[str], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Web Image Capture")
        self._capture_dir = _web_capture_session_dir()
        self._download_dir_saver = download_dir_saver
        self._download_dir_mode_saver = download_dir_mode_saver
        self._browser_detection: BrowserDownloadDetection | None = None
        self._download_mode = (
            str(initial_download_mode or "auto").strip().casefold()
        )
        if self._download_mode not in {"auto", "manual"}:
            self._download_mode = "auto"
        self._external_download_dir = default_downloads_dir()
        requested_dir = str(initial_download_dir or "").strip()
        if self._download_mode == "manual" and requested_dir:
            self._external_download_dir = Path(requested_dir).expanduser()
        else:
            self._browser_detection = detect_browser_download_dir()
            self._external_download_dir = self._browser_detection.download_dir
            self._download_mode = "auto"
        self._external_watcher: ExternalDownloadWatcher | None = None
        self._external_stage_lock = threading.Lock()
        self._selection_callback = selection_callback
        self._captured_files: list[str] = []
        self._thumb_icon_cache: dict[str, QIcon] = {}
        self._tearing_down = False

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Open your browser, download images, then pick one from the captured list."
            )
        )

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit(_google_image_search_url(query), self)
        url_row.addWidget(self.url_edit, 1)
        self.open_btn = QPushButton("Open External", self)
        self.open_btn.clicked.connect(self._open_url)
        url_row.addWidget(self.open_btn)
        self.open_external_btn = QPushButton("Open External + Capture", self)
        self.open_external_btn.clicked.connect(self._toggle_external_capture)
        self.open_external_btn.setToolTip("")
        url_row.addWidget(self.open_external_btn)
        layout.addLayout(url_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Downloads Folder:", self))
        self.download_dir_edit = QLineEdit(str(self._external_download_dir), self)
        self.download_dir_edit.editingFinished.connect(self._on_download_dir_edit_committed)
        folder_row.addWidget(self.download_dir_edit, 1)
        self.browse_download_dir_btn = QPushButton("Browse...", self)
        self.browse_download_dir_btn.clicked.connect(self._on_browse_download_dir)
        folder_row.addWidget(self.browse_download_dir_btn)
        self.detect_download_dir_btn = QPushButton("Detect Browser Default", self)
        self.detect_download_dir_btn.clicked.connect(self._on_detect_browser_default_download_dir)
        folder_row.addWidget(self.detect_download_dir_btn)
        layout.addLayout(folder_row)
        self.detected_browser_label = QLabel(self)
        self.detected_browser_label.setWordWrap(True)
        layout.addWidget(self.detected_browser_label)

        mode_note = QLabel(
            "Embedded browser capture is disabled. "
            "Use external browser capture for stability.",
            self,
        )
        mode_note.setWordWrap(True)
        layout.addWidget(mode_note)

        self.download_table = QTableWidget(0, 4, self)
        self.download_table.setHorizontalHeaderLabels(
            ["Preview", "File", "Size", "Status"]
        )
        self.download_table.verticalHeader().setVisible(False)
        self.download_table.horizontalHeader().setStretchLastSection(True)
        self.download_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.download_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.download_table.setIconSize(QSize(64, 64))
        self.download_table.itemDoubleClicked.connect(
            lambda _item: self._keep_selected_capture(close_after=True)
        )
        layout.addWidget(self.download_table)

        self.status_label = QLabel("Captured images: 0", self)
        layout.addWidget(self.status_label)
        self.auto_focus_check = QCheckBox("Auto-focus on new capture", self)
        self.auto_focus_check.setChecked(True)
        self.auto_focus_check.setToolTip(
            "When enabled, bring this window to front when a new downloaded image is captured."
        )
        layout.addWidget(self.auto_focus_check)

        buttons = QDialogButtonBox(self)
        self.keep_selected_btn = QPushButton("Keep Selected")
        self.keep_all_btn = QPushButton("Keep All")
        self.done_btn = QPushButton("Close Browser")
        self.cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.keep_selected_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.keep_all_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.done_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.keep_selected_btn.clicked.connect(
            lambda: self._keep_selected_capture(close_after=True)
        )
        self.keep_all_btn.clicked.connect(self._keep_all_captures)
        self.done_btn.clicked.connect(self._validate_then_accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self._external_poll_timer = QTimer(self)
        self._external_poll_timer.setInterval(600)
        self._external_poll_timer.timeout.connect(self._poll_external_updates)
        self._external_poll_timer.start()

        self._refresh_detected_browser_label()
        self._refresh_open_external_tooltip()
        self._ingest_existing_capture_files()
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        QTimer.singleShot(0, self._auto_start_external_capture)

    def _refresh_capture_status_label(self) -> None:
        text = f"Captured images: {len(self._captured_files)}"
        if self._external_watcher is not None and self._external_watcher.is_running():
            text += f" | External capture active ({self._external_download_dir})"
        self.status_label.setText(text)

    def _poll_external_updates(self) -> None:
        try:
            watcher = self._external_watcher
            if watcher is None or not watcher.is_running():
                return
            self._ingest_external_captures(watcher.pop_new_paths())
        except Exception:
            return

    def _refresh_detected_browser_label(self) -> None:
        if self._download_mode == "manual":
            self.detected_browser_label.setText(
                f"Manual folder override active: {self.download_dir_edit.text().strip()}"
            )
            return
        detection = self._browser_detection
        if detection is None:
            self.detected_browser_label.setText("Browser default detection: unavailable.")
            return
        self.detected_browser_label.setText(
            "Detected browser default: "
            f"{detection.browser_label} | {detection.download_dir} ({detection.source})"
        )

    def _refresh_open_external_tooltip(self) -> None:
        self.open_external_btn.setToolTip(
            "Open in your default browser and capture new image downloads from "
            f"{self._external_download_dir}"
        )

    def _on_browse_download_dir(self) -> None:
        current = self.download_dir_edit.text().strip()
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Browser Downloads Folder",
            current or str(default_downloads_dir()),
        )
        if not selected:
            return
        self.download_dir_edit.setText(selected)
        self._external_download_dir = Path(selected)
        self._download_mode = "manual"
        self._browser_detection = None
        self._refresh_detected_browser_label()
        self._refresh_open_external_tooltip()
        self._persist_download_dir_settings(selected)

    def _on_detect_browser_default_download_dir(self) -> None:
        detection = detect_browser_download_dir()
        self._browser_detection = detection
        self._download_mode = "auto"
        self._external_download_dir = detection.download_dir
        self.download_dir_edit.setText(str(detection.download_dir))
        self._refresh_detected_browser_label()
        self._refresh_open_external_tooltip()
        self._persist_download_dir_settings(str(detection.download_dir))

    def _on_download_dir_edit_committed(self) -> None:
        text = self.download_dir_edit.text().strip()
        if not text:
            return
        self._external_download_dir = Path(text).expanduser()
        self._download_mode = "manual"
        self._browser_detection = None
        self._refresh_detected_browser_label()
        self._refresh_open_external_tooltip()
        self._persist_download_dir_settings(text)

    def _persist_download_dir_settings(self, path: str) -> None:
        normalized = str(path or "").strip()
        if normalized and self._download_dir_saver is not None:
            try:
                self._download_dir_saver(normalized)
            except Exception:
                pass
        if self._download_dir_mode_saver is not None:
            try:
                self._download_dir_mode_saver(self._download_mode)
            except Exception:
                pass

    def _resolve_external_download_dir_from_ui(self) -> Path | None:
        text = self.download_dir_edit.text().strip()
        if not text:
            return None
        return Path(text).expanduser()

    def _selected_capture_path(self) -> str | None:
        row = self.download_table.currentRow()
        if row < 0:
            return None
        file_item = self.download_table.item(row, 1)
        preview_item = self.download_table.item(row, 0)
        item = file_item or preview_item
        if item is None and preview_item is None:
            return None
        path = str(
            (item.data(Qt.ItemDataRole.UserRole) if item is not None else "") or ""
        ).strip()
        if not path and preview_item is not None and preview_item is not item:
            path = str(preview_item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if path:
            resolved = self._resolve_existing_capture_path(path)
            if resolved is not None:
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, resolved)
                if preview_item is not None:
                    preview_item.setData(Qt.ItemDataRole.UserRole, resolved)
                return resolved
        fallback_name = (file_item.text().strip() if file_item is not None else "").strip()
        if fallback_name:
            resolved = self._resolve_existing_capture_by_name(fallback_name)
            if resolved is not None:
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, resolved)
                if preview_item is not None:
                    preview_item.setData(Qt.ItemDataRole.UserRole, resolved)
                return resolved
        return None

    def _keep_selected_capture(self, close_after: bool) -> None:
        path = self._selected_capture_path()
        if not path:
            QMessageBox.information(
                self,
                "No Selection",
                "Select a completed captured image first.",
            )
            return
        if self._selection_callback is not None:
            try:
                self._selection_callback(path)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Keep Selected Failed",
                    f"Could not keep selected image:\n{exc}",
                )
                return
        if close_after:
            self._validate_then_accept()

    def _keep_all_captures(self) -> None:
        selected_path = self._selected_capture_path()
        if selected_path is None:
            existing = self.captured_files()
            if existing:
                selected_path = existing[-1]
        if selected_path and self._selection_callback is not None:
            try:
                self._selection_callback(selected_path)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Keep All Failed",
                    f"Could not keep captured images:\n{exc}",
                )
                return
        self._validate_then_accept()

    def _open_url(self) -> None:
        target = self.url_edit.text().strip()
        if not target:
            return
        self._open_external_browser()

    def _open_external_browser(self) -> None:
        target = self.url_edit.text().strip()
        if not target:
            return
        try:
            webbrowser.open(target, new=1)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Open External Browser Failed",
                f"Could not open your default browser:\n{exc}",
            )

    def _toggle_external_capture(self) -> None:
        if self._external_watcher is not None and self._external_watcher.is_running():
            self._stop_external_capture(import_results=True)
            return
        resolved = self._resolve_external_download_dir_from_ui()
        if resolved is None:
            QMessageBox.warning(
                self,
                "Download Folder Missing",
                "Choose a valid downloads folder before starting capture.",
            )
            return
        self._external_download_dir = resolved
        if not self._external_download_dir.exists():
            QMessageBox.warning(
                self,
                "Download Folder Missing",
                f"Configured download folder does not exist:\n{self._external_download_dir}",
            )
            return
        self._external_watcher = ExternalDownloadWatcher(
            self._external_download_dir,
            on_detect=self._stage_external_capture,
        )
        started = self._external_watcher.start()
        if not started:
            QMessageBox.warning(
                self,
                "External Capture",
                "External capture watcher is already running.",
            )
            return
        self.open_external_btn.setText("Stop External Capture")
        self.open_external_btn.setToolTip(
            "Stop the external browser capture session and import matching image files."
        )
        self._persist_download_dir_settings(str(self._external_download_dir))
        self._refresh_capture_status_label()
        self._open_external_browser()

    def _auto_start_external_capture(self) -> None:
        if self._tearing_down:
            return
        if self._external_watcher is not None and self._external_watcher.is_running():
            return
        resolved = self._resolve_external_download_dir_from_ui()
        if resolved is None or not resolved.exists():
            self._refresh_capture_status_label()
            return
        self._toggle_external_capture()

    def _stop_external_capture(self, import_results: bool) -> None:
        watcher = self._external_watcher
        if watcher is None:
            return
        self._external_watcher = None
        paths = watcher.stop()
        if import_results:
            self._ingest_external_captures(paths)
        self.open_external_btn.setText("Open External + Capture")
        self._refresh_open_external_tooltip()
        self._refresh_capture_status_label()

    def _stage_external_capture(self, source_path: Path) -> str | None:
        if not source_path.exists() or not _is_supported_image_path(source_path):
            return None
        with self._external_stage_lock:
            target_name = self._unique_name(source_path.name)
            target_path = self._capture_dir / target_name
        for _ in range(4):
            try:
                moved = Path(shutil.move(str(source_path), str(target_path)))
                return str(moved.resolve())
            except OSError:
                time.sleep(0.25)
        try:
            shutil.copy2(str(source_path), str(target_path))
            try:
                source_path.unlink()
            except OSError:
                pass
            return str(target_path.resolve())
        except OSError:
            return None

    def _ingest_external_captures(self, paths: list[str]) -> None:
        existing = {
            Path(path).resolve() for path in self._captured_files if Path(path).exists()
        }
        added = 0
        first_added_row = -1
        for path_str in paths:
            path = Path(path_str)
            if not path.exists() or not _is_supported_image_path(path):
                continue
            resolved = path.resolve()
            if resolved in existing:
                continue
            existing.add(resolved)
            self._captured_files.append(str(resolved))
            row = self.download_table.rowCount()
            self.download_table.insertRow(row)
            self.download_table.setRowHeight(row, 72)
            preview_item = QTableWidgetItem("")
            preview_item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            preview_item.setIcon(self._thumbnail_icon_for_path(path))
            self.download_table.setItem(row, 0, preview_item)
            file_item = QTableWidgetItem(path.name)
            file_item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            self.download_table.setItem(row, 1, file_item)
            try:
                size_text = f"{path.stat().st_size // 1024} KB"
            except OSError:
                size_text = "?"
            self.download_table.setItem(row, 2, QTableWidgetItem(size_text))
            self.download_table.setItem(
                row, 3, QTableWidgetItem("Captured (External, moved)")
            )
            if first_added_row < 0:
                first_added_row = row
            added += 1
        if added:
            self._focus_on_capture_added(first_added_row)
            self._refresh_capture_status_label()

    def _ingest_existing_capture_files(self) -> None:
        if not self._capture_dir.exists():
            return
        existing = [str(path.resolve()) for path in sorted(self._capture_dir.glob("*"))]
        self._ingest_external_captures(existing)

    def _thumbnail_icon_for_path(self, path: Path) -> QIcon:
        try:
            stat = path.stat()
            cache_key = f"{path.resolve()}::{int(stat.st_mtime_ns)}::{int(stat.st_size)}"
        except OSError:
            cache_key = str(path)
        cached = self._thumb_icon_cache.get(cache_key)
        if cached is not None:
            return cached
        pix = QPixmap()
        try:
            payload = _normalize_image_bytes_for_canvas(path.read_bytes())
        except OSError:
            payload = b""
        if payload:
            pix.loadFromData(payload)
        if pix.isNull():
            pix = QPixmap(str(path))
        if pix.isNull():
            icon = QIcon()
            self._thumb_icon_cache[cache_key] = icon
            return icon
        composed = composite_on_checkerboard(
            pix,
            width=64,
            height=64,
            keep_aspect=True,
        )
        icon = QIcon(composed)
        self._thumb_icon_cache[cache_key] = icon
        return icon

    def _focus_on_capture_added(self, row: int) -> None:
        if row >= 0:
            blocked = self.download_table.blockSignals(True)
            self.download_table.selectRow(row)
            self.download_table.blockSignals(blocked)
            self.download_table.scrollToItem(self.download_table.item(row, 1))
        if not self.auto_focus_check.isChecked():
            return
        _bring_window_to_front(self)

    def _unique_name(self, filename: str) -> str:
        candidate = filename
        stem = Path(filename).stem or "image"
        suffix = Path(filename).suffix
        idx = 2
        while (self._capture_dir / candidate).exists():
            candidate = f"{stem}_{idx}{suffix}"
            idx += 1
        return candidate

    def _resolve_existing_capture_path(self, path: str) -> str | None:
        candidate = Path(path)
        if candidate.exists():
            return str(candidate)
        if candidate.name:
            fallback = self._capture_dir / candidate.name
            if fallback.exists():
                return str(fallback)
        return None

    def _resolve_existing_capture_by_name(self, filename: str) -> str | None:
        direct = self._capture_dir / filename
        if direct.exists():
            return str(direct)
        for path_str in reversed(self._captured_files):
            path = Path(path_str)
            if path.exists() and path.name.casefold() == filename.casefold():
                return str(path)
        return None

    def _validate_then_accept(self) -> None:
        self._stop_external_capture(import_results=True)
        if not self._captured_files:
            answer = QMessageBox.question(
                self,
                "No Captured Image",
                "No image download was captured. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def shutdown_session(self) -> None:
        self._shutdown_web_capture(import_results=True)
        self.accept()

    def captured_files(self) -> list[str]:
        return [path for path in self._captured_files if Path(path).exists()]

    def reject(self) -> None:  # type: ignore[override]
        self._shutdown_web_capture(import_results=False)
        super().reject()

    def _shutdown_web_capture(self, import_results: bool) -> None:
        if self._tearing_down:
            return
        self._tearing_down = True
        self._suspend_web_capture(import_results=import_results)

    def _suspend_web_capture(self, import_results: bool) -> None:
        try:
            self._external_poll_timer.stop()
        except Exception:
            pass
        self._stop_external_capture(import_results=import_results)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._shutdown_web_capture(import_results=False)
        super().closeEvent(event)

class SGDBResourcePriorityDialog(QDialog):
    def __init__(
        self,
        resource_order: list[str],
        enabled_resources: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("SteamGridDB Resource Priority")
        self._resource_labels = {value: label for label, value in SGDB_RESOURCE_OPTIONS}
        self._default_order = [value for _, value in SGDB_RESOURCE_OPTIONS]
        initial_order = [value for value in resource_order if value in self._resource_labels]
        for value in self._default_order:
            if value not in initial_order:
                initial_order.append(value)
        initial_enabled = {
            value for value in enabled_resources if value in self._resource_labels
        }
        if not initial_enabled:
            initial_enabled = {"icons", "logos"}

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Drag and drop to reorder request priority. "
                "Check items to enable them for search."
            )
        )
        self.list_widget = QListWidget(self)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for value in initial_order:
            item = QListWidgetItem(self._resource_labels[value], self.list_widget)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            item.setCheckState(
                Qt.CheckState.Checked
                if value in initial_enabled
                else Qt.CheckState.Unchecked
            )
        layout.addWidget(self.list_widget)

        actions = QHBoxLayout()
        self.reset_btn = QPushButton("Reset Defaults", self)
        self.reset_btn.clicked.connect(self._on_reset_defaults)
        actions.addWidget(self.reset_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_then_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(440, 420)

    def _on_reset_defaults(self) -> None:
        self.list_widget.clear()
        for label, value in SGDB_RESOURCE_OPTIONS:
            item = QListWidgetItem(label, self.list_widget)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            default_checked = value in {"icons", "logos"}
            item.setCheckState(
                Qt.CheckState.Checked
                if default_checked
                else Qt.CheckState.Unchecked
            )

    def _validate_then_accept(self) -> None:
        if not self.enabled_resources():
            QMessageBox.warning(
                self,
                "No Resource Enabled",
                "Enable at least one resource type.",
            )
            return
        self.accept()

    def ordered_resources(self) -> list[str]:
        ordered: list[str] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item is None:
                continue
            value = str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold()
            if not value or value in ordered:
                continue
            ordered.append(value)
        return ordered

    def enabled_resources(self) -> set[str]:
        enabled: set[str] = set()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            value = str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold()
            if value:
                enabled.add(value)
        return enabled


class IconPickerDialog(QDialog):
    def __init__(
        self,
        folder_name: str,
        candidates: list[IconCandidate],
        preview_loader,
        image_loader,
        search_callback=None,
        initial_resource_order: list[str] | None = None,
        initial_enabled_resources: set[str] | None = None,
        resource_prefs_saver=None,
        show_cancel_all: bool = False,
        initial_icon_style: str = "none",
        icon_style_saver=None,
        initial_bg_removal_engine: str = "none",
        bg_removal_engine_saver=None,
        initial_background_fill_mode: str = "black",
        initial_background_fill_params: dict[str, object] | None = None,
        background_fill_mode_saver=None,
        initial_border_shader: dict[str, object] | None = None,
        border_shader_saver=None,
        initial_web_download_dir: str | None = None,
        web_download_dir_saver: Callable[[str], None] | None = None,
        initial_web_download_mode: str = "auto",
        web_download_mode_saver: Callable[[str], None] | None = None,
        processing_controls_visible: bool = True,
        size_improvements: dict[int, dict[str, object]] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        _cleanup_web_capture_session()
        self.setWindowTitle(f"Assign Folder Icon - {folder_name}")
        self.folder_name = folder_name
        self.candidates = candidates
        self._preview_loader = preview_loader
        self._image_loader = image_loader
        self._search_callback = search_callback
        self._resource_prefs_saver = resource_prefs_saver
        self._icon_style_saver = icon_style_saver
        self._bg_removal_engine_saver = bg_removal_engine_saver
        self._background_fill_mode_saver = background_fill_mode_saver
        self._border_shader_saver = border_shader_saver
        self._web_download_dir = str(initial_web_download_dir or "").strip()
        self._web_download_dir_saver = web_download_dir_saver
        self._web_download_mode = str(initial_web_download_mode or "auto").strip().casefold()
        if self._web_download_mode not in {"auto", "manual"}:
            self._web_download_mode = "auto"
        self._processing_controls_visible = bool(processing_controls_visible)
        self._web_download_mode_saver = web_download_mode_saver
        self.cancel_all_requested = False
        self._web_capture_dialog: WebDownloadCaptureDialog | None = None
        self._local_image_path: str | None = None
        self._source_image_bytes: bytes | None = None
        self._prepared_image_bytes: bytes | None = None
        self._prepared_is_final_composite = False
        self._local_source_label = "Local File"
        self._local_source_row: int | None = None
        self._size_improvements = dict(size_improvements or {})
        self._preview_pix_cache: dict[tuple[int, int, str], QPixmap] = {}
        self._hover_row: int | None = None
        self._border_shader = border_shader_to_dict(initial_border_shader)
        self._background_fill_params = normalize_background_fill_params(
            initial_background_fill_params
        )
        self._bg_removal_params = normalize_background_removal_params(
            dict(DEFAULT_BG_REMOVAL_PARAMS)
        )
        self._text_preserve_config = text_preserve_to_dict(None)
        default_order = [value for _, value in SGDB_RESOURCE_OPTIONS]
        self._resource_labels = {value: label for label, value in SGDB_RESOURCE_OPTIONS}
        self._resource_order = [
            value
            for value in (initial_resource_order or default_order)
            if value in self._resource_labels
        ]
        for value in default_order:
            if value not in self._resource_order:
                self._resource_order.append(value)
        initial_enabled = {
            value
            for value in (initial_enabled_resources or {"icons", "logos"})
            if value in self._resource_labels
        }
        if not initial_enabled:
            initial_enabled = {"icons", "logos"}
        self._enabled_resources = initial_enabled
        self._last_requested_resources = self._current_requested_resources()
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
                "Template is disabled by default."
            )
        )

        resources_row = QHBoxLayout()
        resources_row.addWidget(QLabel("Resources:", self))
        self.resource_summary = QLabel(self)
        resources_row.addWidget(self.resource_summary, 1)
        self.resource_priority_btn = QPushButton("Priority...", self)
        self.resource_priority_btn.clicked.connect(self._on_manage_resource_priority)
        self.resource_priority_btn.setToolTip("Manage resource order\nShortcut: Alt+P")
        resources_row.addWidget(self.resource_priority_btn)
        self.refresh_candidates_btn = QPushButton("Refresh Results", self)
        self.refresh_candidates_btn.clicked.connect(self._on_refresh_candidates)
        self.refresh_candidates_btn.setToolTip("Refresh results\nShortcut: Ctrl+R")
        resources_row.addWidget(self.refresh_candidates_btn)
        layout.addLayout(resources_row)

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
        self._rebuild_candidates_table(select_first_row=True)
        self.table.setColumnWidth(0, 84)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_source_selection_changed)
        layout.addWidget(self.table)

        options_row = QHBoxLayout()
        self.icon_type_label = QLabel("Icon Type:", self)
        options_row.addWidget(self.icon_type_label)
        self.icon_style_combo = QComboBox(self)
        for label, value in icon_style_options():
            self.icon_style_combo.addItem(label, value)
        default_style = normalize_icon_style(initial_icon_style, circular_ring=False)
        default_idx = self.icon_style_combo.findData(default_style)
        if default_idx >= 0:
            self.icon_style_combo.setCurrentIndex(default_idx)
        self.icon_style_combo.currentIndexChanged.connect(self._on_icon_style_changed)
        options_row.addWidget(self.icon_style_combo)
        self.icon_style_gallery_btn = QPushButton("", self)
        self.icon_style_gallery_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        )
        self.icon_style_gallery_btn.setToolTip("Pick Template from Gallery\nShortcut: Alt+G")
        self.icon_style_gallery_btn.clicked.connect(self._on_pick_icon_style_from_gallery)
        options_row.addWidget(self.icon_style_gallery_btn)
        self.border_shader_btn = QPushButton("", self)
        self.border_shader_btn.setToolTip("Border Shader Controls")
        self.border_shader_btn.clicked.connect(self._on_open_border_shader)
        options_row.addWidget(self.border_shader_btn)
        self.cutout_label = QLabel("Cutout:", self)
        options_row.addWidget(self.cutout_label)
        self.bg_removal_combo = QComboBox(self)
        for label, value in BACKGROUND_REMOVAL_OPTIONS:
            self.bg_removal_combo.addItem(label, value)
        default_bg = normalize_background_removal_engine(initial_bg_removal_engine)
        bg_idx = self.bg_removal_combo.findData(default_bg)
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        self.bg_removal_combo.currentIndexChanged.connect(self._on_bg_engine_changed)
        options_row.addWidget(self.bg_removal_combo)
        self.fill_label = QLabel("Fill:", self)
        options_row.addWidget(self.fill_label)
        self.background_fill_combo = QComboBox(self)
        for label, value in BACKGROUND_FILL_MODE_OPTIONS:
            self.background_fill_combo.addItem(label, value)
        fill_idx = self.background_fill_combo.findData(
            normalize_background_fill_mode(initial_background_fill_mode)
        )
        if fill_idx >= 0:
            self.background_fill_combo.setCurrentIndex(fill_idx)
        self.background_fill_combo.currentIndexChanged.connect(
            self._on_background_fill_mode_changed
        )
        options_row.addWidget(self.background_fill_combo)
        self.text_label = QLabel("Text:", self)
        options_row.addWidget(self.text_label)
        self.text_extract_combo = QComboBox(self)
        for label, value in TEXT_EXTRACTION_METHOD_OPTIONS:
            self.text_extract_combo.addItem(label, value)
        text_idx = self.text_extract_combo.findData(
            normalize_text_extraction_method(
                str(self._text_preserve_config.get("method", "") or ""),
                enabled_fallback=bool(self._text_preserve_config.get("enabled", False)),
            )
        )
        if text_idx >= 0:
            self.text_extract_combo.setCurrentIndex(text_idx)
        self.text_extract_combo.currentIndexChanged.connect(self._on_text_extract_method_changed)
        options_row.addWidget(self.text_extract_combo)
        options_row.addStretch(1)
        self.local_btn = QPushButton("Use Local Image...")
        self.local_btn.setToolTip("Choose local image\nShortcut: Ctrl+O")
        self.local_btn.clicked.connect(self._on_pick_local)
        options_row.addWidget(self.local_btn)
        self.web_btn = QPushButton("Web Capture...")
        self.web_btn.setToolTip("Open web capture\nShortcut: Alt+W")
        self.web_btn.clicked.connect(self._on_pick_web_capture)
        options_row.addWidget(self.web_btn)
        self.frame_btn = QPushButton("Adjust Framing...")
        self.frame_btn.setToolTip("Adjust framing and apply\nShortcut: Alt+F")
        self.frame_btn.clicked.connect(self._on_adjust_framing)
        options_row.addWidget(self.frame_btn)
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
        if show_cancel_all:
            self.cancel_all_btn = QPushButton("Cancel All", self)
            self.cancel_all_btn.setToolTip("Cancel all remaining entries\nShortcut: Alt+C")
            self.cancel_all_btn.clicked.connect(self._on_cancel_all)
            buttons.addButton(self.cancel_all_btn, QDialogButtonBox.ButtonRole.RejectRole)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setToolTip("Accept selection\nShortcut: Ctrl+Enter")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setToolTip("Cancel\nShortcut: Esc")
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        self._refresh_border_shader_button()
        if not self._processing_controls_visible:
            none_idx = self.icon_style_combo.findData("none")
            if none_idx >= 0:
                self.icon_style_combo.setCurrentIndex(none_idx)
            bg_none_idx = self.bg_removal_combo.findData("none")
            if bg_none_idx >= 0:
                self.bg_removal_combo.setCurrentIndex(bg_none_idx)
            text_none_idx = self.text_extract_combo.findData("none")
            if text_none_idx >= 0:
                self.text_extract_combo.setCurrentIndex(text_none_idx)
            self._text_preserve_config = text_preserve_to_dict(
                {"enabled": False, "method": "none"}
            )
            self._border_shader = border_shader_to_dict({"enabled": False})
            for widget in (
                self.icon_type_label,
                self.icon_style_combo,
                self.icon_style_gallery_btn,
                self.border_shader_btn,
                self.cutout_label,
                self.bg_removal_combo,
                self.fill_label,
                self.background_fill_combo,
                self.text_label,
                self.text_extract_combo,
            ):
                widget.setVisible(False)
        self._sync_template_dependents()
        self._update_resource_summary()
        self._update_refresh_candidates_button()
        _bind_dialog_shortcut(self, "Ctrl+O", self._on_pick_local)
        _bind_dialog_shortcut(self, "Alt+W", self._on_pick_web_capture)
        _bind_dialog_shortcut(self, "Alt+F", self._on_adjust_framing)
        _bind_dialog_shortcut(self, "Ctrl+R", self._on_refresh_candidates)
        _bind_dialog_shortcut(self, "Alt+P", self._on_manage_resource_priority)
        if self._processing_controls_visible:
            _bind_dialog_shortcut(self, "Alt+G", self._on_pick_icon_style_from_gallery)
        _bind_dialog_shortcut(self, "Ctrl+Return", self._validate_then_accept)
        _bind_dialog_shortcut(self, "Ctrl+Enter", self._validate_then_accept)
        if show_cancel_all:
            _bind_dialog_shortcut(self, "Alt+C", self._on_cancel_all)
        _bind_dialog_shortcut(self, "F1", self._show_shortcuts)
        if self._search_callback is None:
            self.resource_priority_btn.setEnabled(False)
            self.refresh_candidates_btn.setEnabled(False)
            self.refresh_candidates_btn.setToolTip("Refreshing sources is unavailable.")

    def _on_cancel_all(self) -> None:
        self.cancel_all_requested = True
        self.reject()

    def _show_shortcuts(self) -> None:
        lines = [
            "Ctrl+O - Use local image",
            "Alt+W - Web capture",
            "Alt+F - Adjust framing",
            "Ctrl+R - Refresh results",
            "Alt+P - Resource priority",
            "Ctrl+Enter - Accept selection",
            "Esc - Cancel dialog",
            "F1 - Show shortcuts",
        ]
        if self._processing_controls_visible:
            lines.insert(5, "Alt+G - Open template gallery")
        if hasattr(self, "cancel_all_btn"):
            lines.insert(-2, "Alt+C - Cancel all")
        QMessageBox.information(self, "Icon Picker Shortcuts", "\n".join(lines))

    def _rebuild_candidates_table(self, select_first_row: bool = False) -> None:
        selected_row = self.table.currentRow()
        self._hide_hover_preview()
        self._preview_pix_cache.clear()
        self.table.clearContents()
        self.table.setRowCount(len(self.candidates))
        for row, candidate in enumerate(self.candidates):
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
        self._local_source_row = None
        if self._source_image_bytes:
            local_path = self._local_image_path or ""
            self._upsert_local_source_row(
                local_path,
                self._source_image_bytes,
                self._local_source_label,
            )
        self.table.setColumnWidth(0, 84)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        target_row = selected_row
        if select_first_row and self.table.rowCount() > 0:
            target_row = 0
        if target_row < 0 and self.table.rowCount() > 0:
            target_row = 0
        if 0 <= target_row < self.table.rowCount():
            blocked = self.table.blockSignals(True)
            self.table.clearSelection()
            self.table.selectRow(target_row)
            self.table.blockSignals(blocked)

    def _current_requested_resources(self) -> list[str]:
        return [
            value for value in self._resource_order if value in self._enabled_resources
        ]

    def _update_resource_summary(self) -> None:
        enabled = self._current_requested_resources()
        if enabled:
            labels = [self._resource_labels.get(value, value.title()) for value in enabled]
            summary = " > ".join(labels)
        else:
            summary = "(none enabled)"
        self.resource_summary.setText(summary)

    def _update_refresh_candidates_button(self) -> None:
        changed = self._current_requested_resources() != self._last_requested_resources
        if changed:
            self.refresh_candidates_btn.setStyleSheet(
                "QPushButton { background-color: #6b1d1d; color: #ffffff; font-weight: 600; }"
            )
            self.refresh_candidates_btn.setToolTip(
                "Resource selection changed. Refresh to request updated results."
            )
            return
        self.refresh_candidates_btn.setStyleSheet("")
        self.refresh_candidates_btn.setToolTip("Refresh search with selected resources.")

    def _on_manage_resource_priority(self) -> None:
        dialog = SGDBResourcePriorityDialog(
            resource_order=self._resource_order,
            enabled_resources=self._enabled_resources,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._resource_order = dialog.ordered_resources()
        self._enabled_resources = dialog.enabled_resources()
        self._update_resource_summary()
        if self._resource_prefs_saver is not None:
            try:
                saved = self._resource_prefs_saver(
                    list(self._resource_order), set(self._enabled_resources)
                )
                if isinstance(saved, tuple) and len(saved) == 2:
                    saved_order, saved_enabled = saved
                    if isinstance(saved_order, list):
                        self._resource_order = [
                            value
                            for value in saved_order
                            if value in self._resource_labels
                        ]
                    if isinstance(saved_enabled, set):
                        self._enabled_resources = {
                            value
                            for value in saved_enabled
                            if value in self._resource_labels
                        }
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Save Preferences Failed",
                    f"Could not persist resource preferences:\n{exc}",
                )
        self._update_refresh_candidates_button()

    def _on_refresh_candidates(self) -> None:
        if self._search_callback is None:
            return
        resources = self._current_requested_resources()
        if not resources:
            QMessageBox.warning(
                self,
                "No Resources Selected",
                "Select at least one resource type before refreshing.",
            )
            return
        try:
            refreshed = self._search_callback(resources)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Refresh Failed",
                f"Could not refresh icon candidates:\n{exc}",
            )
            return
        self.candidates = refreshed
        self._last_requested_resources = list(resources)
        self._rebuild_candidates_table(select_first_row=True)
        self._update_resource_summary()
        self._update_refresh_candidates_button()
        if not refreshed:
            QMessageBox.information(
                self,
                "No Results",
                "No candidates found for the selected resource types.",
            )

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
        self._close_web_capture_dialog_async()
        _cleanup_web_capture_session()
        super().closeEvent(event)

    def _close_web_capture_dialog_async(self) -> None:
        dialog = self._web_capture_dialog
        if dialog is None:
            return
        self._web_capture_dialog = None
        try:
            dialog.shutdown_session()
        except Exception:
            try:
                dialog.close()
            except Exception:
                pass

    def _current_icon_style(self) -> str:
        if not hasattr(self, "icon_style_combo"):
            return "none"
        return str(self.icon_style_combo.currentData() or "none")

    def _template_enabled(self) -> bool:
        return self._current_icon_style() != "none"

    def _sync_template_dependents(self) -> None:
        template_enabled = self._template_enabled()
        if not template_enabled:
            bg_idx = self.bg_removal_combo.findData("none")
            if bg_idx >= 0 and self.bg_removal_combo.currentIndex() != bg_idx:
                blocked = self.bg_removal_combo.blockSignals(True)
                self.bg_removal_combo.setCurrentIndex(bg_idx)
                self.bg_removal_combo.blockSignals(blocked)
            text_idx = self.text_extract_combo.findData("none")
            if text_idx >= 0 and self.text_extract_combo.currentIndex() != text_idx:
                blocked = self.text_extract_combo.blockSignals(True)
                self.text_extract_combo.setCurrentIndex(text_idx)
                self.text_extract_combo.blockSignals(blocked)
            fill_idx = self.background_fill_combo.findData("black")
            if fill_idx >= 0 and self.background_fill_combo.currentIndex() != fill_idx:
                blocked = self.background_fill_combo.blockSignals(True)
                self.background_fill_combo.setCurrentIndex(fill_idx)
                self.background_fill_combo.blockSignals(blocked)
            cfg = dict(self._text_preserve_config)
            cfg.update({"enabled": False, "method": "none"})
            self._text_preserve_config = text_preserve_to_dict(cfg)
        self.bg_removal_combo.setEnabled(template_enabled)
        self.background_fill_combo.setEnabled(template_enabled)
        self.text_extract_combo.setEnabled(template_enabled)
        self.border_shader_btn.setEnabled(template_enabled)

    def _current_bg_removal_engine(self) -> str:
        if not hasattr(self, "bg_removal_combo"):
            return "none"
        if not self._template_enabled():
            return "none"
        return str(self.bg_removal_combo.currentData() or "none")

    def _current_background_fill_mode(self) -> str:
        if not hasattr(self, "background_fill_combo"):
            return "black"
        if not self._template_enabled():
            return "black"
        return normalize_background_fill_mode(
            str(self.background_fill_combo.currentData() or "black")
        )

    def _is_heavy_bg_engine(self) -> bool:
        return self._current_bg_removal_engine() in {"rembg", "bria_rmbg"}

    def _preview_bg_engine(self) -> str:
        # Keep UI responsive: heavy cutout engines are only applied at final icon build time.
        if self._is_heavy_bg_engine():
            return "none"
        return self._current_bg_removal_engine()

    def _border_shader_config(self) -> dict[str, object]:
        return dict(self._border_shader)

    def _on_icon_style_changed(self, *_args) -> None:
        self._sync_template_dependents()
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._refresh_preview_icons()
        if self._icon_style_saver is not None:
            try:
                self._icon_style_saver(self._current_icon_style())
            except Exception:
                pass

    def _on_pick_icon_style_from_gallery(self) -> None:
        dialog = TemplateGalleryDialog(
            _icon_style_gallery_entries(),
            current_key=self._current_icon_style(),
            title="Select Template",
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.selected_key()
        idx = self.icon_style_combo.findData(selected)
        if idx >= 0:
            self.icon_style_combo.setCurrentIndex(idx)

    def _on_bg_engine_changed(self, *_args) -> None:
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()
        if self._bg_removal_engine_saver is not None:
            try:
                self._bg_removal_engine_saver(self._current_bg_removal_engine())
            except Exception:
                pass

    def _on_background_fill_mode_changed(self, *_args) -> None:
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()
        if self._background_fill_mode_saver is not None:
            try:
                self._background_fill_mode_saver(self._current_background_fill_mode())
            except Exception:
                pass

    def _on_text_extract_method_changed(self, _index: int) -> None:
        if not self._template_enabled():
            method = "none"
        else:
            method = str(self.text_extract_combo.currentData() or "none")
        cfg = dict(self._text_preserve_config)
        cfg.update({"enabled": method != "none", "method": method})
        self._text_preserve_config = text_preserve_to_dict(cfg)
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()

    def _refresh_border_shader_button(self) -> None:
        cfg = normalize_border_shader_config(self._border_shader)
        color = QColor()
        if cfg.mode == "hsl":
            color.setHsl(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        else:
            color.setHsv(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        self.border_shader_btn.setStyleSheet(
            _shader_swatch_css((color.red(), color.green(), color.blue()))
        )

    def _on_open_border_shader(self) -> None:
        dialog = BorderShaderDialog(
            icon_style=self._current_icon_style(),
            initial_config=self._border_shader,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._border_shader = dialog.result_config()
        self._refresh_border_shader_button()
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()
        if self._border_shader_saver is not None:
            try:
                self._border_shader_saver(dict(self._border_shader))
            except Exception:
                pass

    def _preview_icon(self, row: int, size: int) -> QIcon:
        pix = self._preview_pixmap(row, size)
        if pix is None or pix.isNull():
            return QIcon()
        return QIcon(pix)

    def _preview_pixmap(self, row: int, size: int) -> QPixmap | None:
        if row < 0:
            return None
        if self._local_source_row is not None and row == self._local_source_row:
            return self._styled_local_preview_pixmap(size)
        if row >= len(self.candidates):
            return None
        icon_style = self._current_icon_style()
        bg_engine = self._preview_bg_engine()
        fill_mode = self._current_background_fill_mode()
        shader_key = json.dumps(self._border_shader_config(), sort_keys=True)
        cache_key = (row, size, f"{icon_style}|{bg_engine}|{fill_mode}|{shader_key}")
        cached = self._preview_pix_cache.get(cache_key)
        if cached is not None:
            return cached
        candidate = self.candidates[row]
        try:
            try:
                preview_png = self._preview_loader(
                    candidate,
                    icon_style,
                    size,
                    bg_engine,
                    self._border_shader_config(),
                    fill_mode,
                    dict(self._background_fill_params),
                )
            except TypeError:
                preview_png = self._preview_loader(
                    candidate,
                    icon_style,
                    size,
                    bg_engine,
                    self._border_shader_config(),
                )
            pix = QPixmap()
            if not pix.loadFromData(preview_png):
                return None
            composed = composite_on_checkerboard(
                pix,
                width=size,
                height=size,
                keep_aspect=True,
            )
            self._preview_pix_cache[cache_key] = composed
            return composed
        except Exception:
            return None

    def _styled_local_preview_pixmap(self, size: int) -> QPixmap | None:
        local_bytes = self._prepared_image_bytes or self._source_image_bytes
        if not local_bytes:
            return None
        if self._prepared_is_final_composite and self._prepared_image_bytes:
            pix = QPixmap()
            if not pix.loadFromData(self._prepared_image_bytes):
                return None
            return composite_on_checkerboard(
                pix,
                width=size,
                height=size,
                keep_aspect=True,
            )
        try:
            preview_png = build_preview_png(
                local_bytes,
                size=size,
                icon_style=self._current_icon_style(),
                bg_removal_engine=self._preview_bg_engine(),
                text_preserve_config=self._text_preserve_config,
                border_shader=self._border_shader_config(),
                background_fill_mode=self._current_background_fill_mode(),
                background_fill_params=dict(self._background_fill_params),
            )
            pix = QPixmap()
            if pix.loadFromData(preview_png):
                return composite_on_checkerboard(
                    pix,
                    width=size,
                    height=size,
                    keep_aspect=True,
                )
        except Exception:
            pass
        pix = QPixmap()
        if not pix.loadFromData(local_bytes):
            return None
        return composite_on_checkerboard(
            pix,
            width=size,
            height=size,
            keep_aspect=True,
        )

    def _refresh_preview_icon_row(self, row: int) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        item.setIcon(self._preview_icon(row, 64))

    def _refresh_preview_icons(self, *, lazy: bool = False) -> None:
        self._hide_hover_preview()
        if lazy:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is None:
                    continue
                item.setIcon(QIcon())
            current = self.table.currentRow()
            if current >= 0:
                self._refresh_preview_icon_row(current)
            elif self.table.rowCount() > 0:
                self._refresh_preview_icon_row(0)
            if self._local_source_row is not None:
                self._refresh_preview_icon_row(self._local_source_row)
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            item.setIcon(self._preview_icon(row, 64))

    def _upsert_local_source_row(self, path: str, bytes_data: bytes, source_label: str) -> None:
        if self._local_source_row is None:
            self._local_source_row = self.table.rowCount()
            self.table.insertRow(self._local_source_row)
            self.table.setRowHeight(self._local_source_row, 72)

        row = self._local_source_row
        pix = self._styled_local_preview_pixmap(64) or QPixmap()
        preview_item = QTableWidgetItem("")
        if not pix.isNull():
            preview_item.setIcon(QIcon(pix))
        self.table.setItem(row, 0, preview_item)
        self.table.setItem(row, 1, QTableWidgetItem("Selected File"))
        self.table.setItem(row, 2, QTableWidgetItem(source_label))
        if pix.isNull():
            size_text = "unknown"
        else:
            size_text = f"{pix.width()}x{pix.height()}"
        self.table.setItem(row, 3, QTableWidgetItem(size_text))
        source_item = QTableWidgetItem(path)
        source_item.setToolTip(path)
        self.table.setItem(row, 4, source_item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        was_blocked = self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.selectRow(row)
        self.table.blockSignals(was_blocked)
        self.table.scrollToItem(self.table.item(row, 0))

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
            "Images (*.png *.jpg *.jpe *.jpeg *.jfif *.avif *.webp *.bmp);;All Files (*)",
        )
        if not selected:
            return
        self._set_local_source(selected, "Local File")

    def _on_pick_web_capture(self) -> None:
        if self._web_capture_dialog is not None:
            self._web_capture_dialog.show()
            self._web_capture_dialog.raise_()
            self._web_capture_dialog.activateWindow()
            return
        query = f"{self.folder_name} game icon"
        browser = WebDownloadCaptureDialog(
            query,
            self,
            selection_callback=self._on_web_capture_image_selected,
            initial_download_dir=self._web_download_dir,
            download_dir_saver=self._on_web_download_dir_changed,
            initial_download_mode=self._web_download_mode,
            download_dir_mode_saver=self._on_web_download_mode_changed,
        )
        browser.setWindowModality(Qt.WindowModality.NonModal)
        browser.finished.connect(self._on_web_capture_dialog_finished)
        self._web_capture_dialog = browser
        browser.show()
        browser.raise_()
        browser.activateWindow()

    def _on_web_capture_dialog_finished(self, _result: int) -> None:
        self._web_capture_dialog = None

    def _on_web_capture_image_selected(self, path: str) -> None:
        self._set_local_source(path, "Web Download")

    def _on_web_download_dir_changed(self, path: str) -> None:
        normalized = str(path or "").strip()
        if not normalized:
            return
        self._web_download_dir = normalized
        if self._web_download_dir_saver is not None:
            try:
                self._web_download_dir_saver(normalized)
            except Exception:
                pass

    def _on_web_download_mode_changed(self, mode: str) -> None:
        normalized = str(mode or "").strip().casefold()
        if normalized not in {"auto", "manual"}:
            return
        self._web_download_mode = normalized
        if self._web_download_mode_saver is not None:
            try:
                self._web_download_mode_saver(normalized)
            except Exception:
                pass

    def _set_local_source(self, path: str, source_label: str) -> None:
        image_bytes: bytes
        try:
            image_bytes = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(
                self, "Image Read Failed", f"Could not read selected image:\n{exc}"
            )
            return
        image_bytes = _normalize_image_bytes_for_canvas(image_bytes)
        self._local_image_path = path
        self._local_source_label = source_label
        self._source_image_bytes = image_bytes
        self._prepared_image_bytes = None
        self._prepared_is_final_composite = False
        self._upsert_local_source_row(path, image_bytes, source_label)

    def _resolve_current_image_bytes(self) -> bytes | None:
        row = self.table.currentRow()
        if self._local_source_row is not None and row == self._local_source_row:
            return self._prepared_image_bytes or self._source_image_bytes
        if 0 <= row < len(self.candidates):
            try:
                return self._image_loader(self.candidates[row])
            except Exception as exc:
                QMessageBox.warning(
                    self, "Image Download Failed", f"Could not download selected image:\n{exc}"
                )
                return None
        if self._prepared_image_bytes:
            return self._prepared_image_bytes
        if self._source_image_bytes:
            return self._source_image_bytes
        if self._local_image_path:
            try:
                return Path(self._local_image_path).read_bytes()
            except OSError as exc:
                QMessageBox.warning(
                    self, "Image Read Failed", f"Could not read local image:\n{exc}"
                )
                return None
        return None

    def _on_adjust_framing(self) -> None:
        image_bytes = self._resolve_current_image_bytes()
        if image_bytes is None:
            QMessageBox.information(
                self,
                "No Image Source",
                "Select a candidate, local image, or captured web image first.",
            )
            return
        dialog = IconFramingDialog(
            image_bytes,
            border_style=self._current_icon_style(),
            initial_bg_removal_engine=self._current_bg_removal_engine(),
            initial_bg_removal_params=dict(self._bg_removal_params),
            initial_text_preserve_config=dict(self._text_preserve_config),
            initial_background_fill_mode=self._current_background_fill_mode(),
            initial_background_fill_params=dict(self._background_fill_params),
            border_shader=self._border_shader_config(),
            size_improvements=self._size_improvements,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        style_idx = self.icon_style_combo.findData(dialog.selected_style())
        if style_idx >= 0:
            self.icon_style_combo.setCurrentIndex(style_idx)
        bg_idx = self.bg_removal_combo.findData(dialog.selected_bg_removal_engine())
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        fill_idx = self.background_fill_combo.findData(dialog.selected_background_fill_mode())
        if fill_idx >= 0:
            self.background_fill_combo.setCurrentIndex(fill_idx)
        self._background_fill_params = dialog.selected_background_fill_params()
        self._bg_removal_params = dialog.selected_bg_removal_params()
        self._text_preserve_config = dialog.selected_text_preserve_config()
        text_idx = self.text_extract_combo.findData(
            str(self._text_preserve_config.get("method", "none") or "none")
        )
        if text_idx >= 0:
            self.text_extract_combo.setCurrentIndex(text_idx)
        self._border_shader = dialog.selected_border_shader()
        self._refresh_border_shader_button()
        if self._border_shader_saver is not None:
            try:
                self._border_shader_saver(dict(self._border_shader))
            except Exception:
                pass
        framed = dialog.framed_image_bytes()
        if not framed:
            return
        self._prepared_image_bytes = framed
        self._prepared_is_final_composite = dialog.framed_image_is_final_composite()
        self._source_image_bytes = framed
        local_path = self._local_image_path or "(framed image)"
        label = (
            f"{self._local_source_label} (Framed)"
            if self._local_source_label
            else "Framed"
        )
        self._upsert_local_source_row(local_path, framed, label)
        # Applying framing in Set Icon flow should immediately finalize selection.
        self._validate_then_accept()

    def _on_source_selection_changed(self) -> None:
        current_row = self.table.currentRow()
        if self._is_heavy_bg_engine() and current_row >= 0:
            self._refresh_preview_icon_row(current_row)
        if 0 <= current_row < len(self.candidates):
            self._local_image_path = None
            self._source_image_bytes = None
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False

    def _validate_then_accept(self) -> None:
        row = self.table.currentRow()
        has_local = self._local_image_path is not None or self._source_image_bytes is not None
        if row < 0 and not has_local and self._prepared_image_bytes is None:
            QMessageBox.warning(
                self,
                "No Selection",
                "Select one candidate row or choose a local image.",
            )
            return
        if (
            self._local_image_path
            and self._source_image_bytes is None
            and not Path(self._local_image_path).exists()
        ):
            QMessageBox.warning(
                self,
                "Missing File",
                "The selected local/captured file no longer exists.",
            )
            return
        self._close_web_capture_dialog_async()
        self.accept()

    def result_payload(self) -> IconPickerResult:
        row = self.table.currentRow()
        candidate = self.candidates[row] if 0 <= row < len(self.candidates) else None
        return IconPickerResult(
            candidate=candidate,
            local_image_path=self._local_image_path,
            source_image_bytes=self._source_image_bytes,
            prepared_image_bytes=self._prepared_image_bytes,
            prepared_is_final_composite=self._prepared_is_final_composite,
            info_tip=self.info_tip_edit.toPlainText().strip(),
            icon_style=self._current_icon_style(),
            bg_removal_engine=self._current_bg_removal_engine(),
            bg_removal_params=dict(self._bg_removal_params),
            text_preserve_config=dict(self._text_preserve_config),
            border_shader=self._border_shader_config(),
            background_fill_mode=self._current_background_fill_mode(),
            background_fill_params=dict(self._background_fill_params),
        )


__all__ = [
    "IconPickerDialog",
    "SGDBResourcePriorityDialog",
]
