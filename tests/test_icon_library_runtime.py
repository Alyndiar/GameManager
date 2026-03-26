from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import types

from gamemanager.services.browser_downloads import BrowserDownloadDetection
from gamemanager.ui.dialogs import icon_library


@dataclass
class _FakeHandle:
    activated: int = 0

    def requestActivate(self) -> None:  # noqa: N802
        self.activated += 1


class _FakeWidget:
    def __init__(self, minimized: bool = False, hwnd: int = 123):
        self._minimized = minimized
        self._hwnd = hwnd
        self.calls: list[str] = []
        self.handle = _FakeHandle()

    def isMinimized(self) -> bool:  # noqa: N802
        return self._minimized

    def showNormal(self) -> None:  # noqa: N802
        self.calls.append("showNormal")

    def show(self) -> None:
        self.calls.append("show")

    def raise_(self) -> None:
        self.calls.append("raise")

    def activateWindow(self) -> None:  # noqa: N802
        self.calls.append("activateWindow")

    def windowHandle(self) -> _FakeHandle:  # noqa: N802
        return self.handle

    def winId(self) -> int:  # noqa: N802
        return self._hwnd


def test_bring_window_to_front_invokes_qt_and_win32_paths(monkeypatch) -> None:
    widget = _FakeWidget(minimized=True, hwnd=42)
    calls: list[tuple[str, int]] = []

    class _User32:
        def ShowWindow(self, hwnd: int, cmd: int) -> None:  # noqa: N802
            calls.append(("ShowWindow", hwnd))

        def BringWindowToTop(self, hwnd: int) -> None:  # noqa: N802
            calls.append(("BringWindowToTop", hwnd))

        def SetForegroundWindow(self, hwnd: int) -> None:  # noqa: N802
            calls.append(("SetForegroundWindow", hwnd))

        def SetWindowPos(self, hwnd: int, *_args) -> None:  # noqa: N802
            calls.append(("SetWindowPos", hwnd))

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(user32=_User32())
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr(icon_library.os, "name", "nt", raising=False)

    icon_library._bring_window_to_front(widget)

    assert widget.calls[:4] == ["showNormal", "show", "raise", "activateWindow"]
    assert widget.handle.activated == 1
    assert ("ShowWindow", 42) in calls
    assert ("BringWindowToTop", 42) in calls
    assert ("SetForegroundWindow", 42) in calls
    assert len([entry for entry in calls if entry[0] == "SetWindowPos"]) == 2


def test_external_download_watcher_final_pass_and_pop_new_paths(tmp_path: Path) -> None:
    watcher = icon_library.ExternalDownloadWatcher(tmp_path, poll_seconds=10.0)
    assert watcher.start() is True
    assert watcher.start() is False
    created = tmp_path / "capture.png"
    created.write_bytes(b"png")

    captured = watcher.stop()
    assert str(created.resolve()) in captured

    first_pop = watcher.pop_new_paths()
    assert str(created.resolve()) in first_pop
    assert watcher.pop_new_paths() == []


def test_external_download_watcher_filters_nonexistent_detected_paths(tmp_path: Path) -> None:
    def _fake_detect(_path: Path) -> str:
        return str(tmp_path / "does_not_exist.png")

    watcher = icon_library.ExternalDownloadWatcher(
        tmp_path, poll_seconds=10.0, on_detect=_fake_detect
    )
    assert watcher.start() is True
    created = tmp_path / "source.png"
    created.write_bytes(b"png")
    captured = watcher.stop()

    assert captured == []
    assert watcher.pop_new_paths() == []


def test_web_capture_download_mode_persistence_and_focus_hook(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir(parents=True, exist_ok=True)

    mode_saves: list[str] = []
    dir_saves: list[str] = []

    monkeypatch.setattr(icon_library, "_web_capture_session_dir", lambda: session_dir)
    monkeypatch.setattr(icon_library, "default_downloads_dir", lambda: tmp_path / "missing_default")
    monkeypatch.setattr(
        icon_library,
        "detect_browser_download_dir",
        lambda: BrowserDownloadDetection(
            browser_id="edge",
            browser_label="Microsoft Edge",
            download_dir=auto_dir,
            source="test",
        ),
    )

    dialog = icon_library.WebDownloadCaptureDialog(
        query="test",
        initial_download_mode="manual",
        initial_download_dir=str(tmp_path / "missing_manual"),
        download_dir_saver=dir_saves.append,
        download_dir_mode_saver=mode_saves.append,
    )
    qtbot.addWidget(dialog)

    dialog.download_dir_edit.setText(str(manual_dir))
    dialog._on_download_dir_edit_committed()
    assert mode_saves[-1] == "manual"
    assert dir_saves[-1] == str(manual_dir)

    dialog._on_detect_browser_default_download_dir()
    assert mode_saves[-1] == "auto"
    assert dir_saves[-1] == str(auto_dir)

    focus_calls: list[object] = []
    monkeypatch.setattr(icon_library, "_bring_window_to_front", lambda widget: focus_calls.append(widget))
    dialog.auto_focus_check.setChecked(True)
    dialog._focus_on_capture_added(-1)
    assert focus_calls == [dialog]
    dialog.auto_focus_check.setChecked(False)
    dialog._focus_on_capture_added(-1)
    assert focus_calls == [dialog]

    dialog._shutdown_web_capture(import_results=False)
