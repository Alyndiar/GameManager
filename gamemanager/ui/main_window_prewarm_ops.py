from __future__ import annotations

import json
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QTimer

from gamemanager.services.background_removal import normalize_background_removal_engine


class MainWindowPrewarmOpsMixin:
    def _start_ml_prewarm(self) -> None:
        self._prewarm_scheduled = False
        if self._ml_prewarm_started or self._prewarm_in_progress:
            return
        mode = self._startup_prewarm_mode()
        if mode == "off":
            self._ml_prewarm_started = True
            return
        resources = self._prewarm_resource_ids_for_mode(mode)
        if not resources:
            self._ml_prewarm_started = True
            return
        self._ml_prewarm_started = True
        self._prewarm_in_progress = True
        self._set_background_progress("Preload models", 0, len(resources))
        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments(
            [
                "-m",
                "gamemanager.services.prewarm_subprocess",
                "--worker",
                "--resources-json",
                json.dumps(resources, ensure_ascii=False),
            ]
        )
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        self._prewarm_stdout_buffer = ""
        self._prewarm_stderr_buffer = ""
        process.readyReadStandardOutput.connect(self._on_prewarm_stdout_ready)
        process.readyReadStandardError.connect(self._on_prewarm_stderr_ready)
        process.errorOccurred.connect(self._on_prewarm_process_error)
        process.finished.connect(self._on_prewarm_process_finished)
        self._prewarm_process = process
        process.start()

    def _startup_prewarm_mode(self) -> str:
        value = self.state.get_ui_pref("perf_startup_prewarm_mode", "minimal").strip().casefold()
        if value in {"off", "minimal", "full"}:
            return value
        return "minimal"

    def _prewarm_resource_ids_for_mode(self, mode: str) -> list[str]:
        mode_key = str(mode or "minimal").strip().casefold()
        if mode_key == "off":
            return []
        if mode_key == "full":
            return ["torch_runtime", "background_stack", "text_stack"]

        resources = ["torch_runtime"]
        bg_engine = normalize_background_removal_engine(
            self.state.get_ui_pref("icon_bg_removal_engine", "none")
        )
        if bg_engine == "rembg":
            resources.append("background_rembg")
        elif bg_engine == "bria_rmbg":
            resources.append("background_bria")

        text_method = self.state.get_ui_pref("icon_text_extraction_method", "none").strip().casefold()
        if text_method == "paddleocr":
            resources.append("text_paddle")
        elif text_method == "opencv_db":
            resources.append("text_opencv")

        seen: set[str] = set()
        ordered: list[str] = []
        for item in resources:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def _schedule_startup_prewarm_if_ready(self) -> None:
        if self._ml_prewarm_started or self._prewarm_in_progress or self._prewarm_scheduled:
            return
        if not self._first_show_done or not self._initial_refresh_done:
            return
        if self._startup_prewarm_mode() == "off":
            self._ml_prewarm_started = True
            return
        self._prewarm_scheduled = True
        self._set_background_progress("Preload scheduled", 0, 1)
        self._prewarm_delay_timer.start(1500)

    def _on_prewarm_stdout_ready(self) -> None:
        if self._prewarm_process is None:
            return
        text = bytes(self._prewarm_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not text:
            return
        self._prewarm_stdout_buffer += text
        lines = self._prewarm_stdout_buffer.splitlines()
        if self._prewarm_stdout_buffer and not self._prewarm_stdout_buffer.endswith(("\n", "\r")):
            self._prewarm_stdout_buffer = lines.pop() if lines else self._prewarm_stdout_buffer
        else:
            self._prewarm_stdout_buffer = ""
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(payload.get("type") or "")
            if kind == "progress":
                self._on_prewarm_progress(
                    str(payload.get("stage") or "Preload"),
                    int(payload.get("current") or 0),
                    int(payload.get("total") or 0),
                )
            elif kind == "done":
                self._on_prewarm_completed(str(payload.get("message") or "Preload complete"))
            elif kind == "error":
                self._on_prewarm_failed(str(payload.get("message") or "Preload failed"))
            elif kind == "warning":
                self._set_background_progress(str(payload.get("message") or "Preload warning"), 1, 1)

    def _on_prewarm_stderr_ready(self) -> None:
        if self._prewarm_process is None:
            return
        text = bytes(self._prewarm_process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            self._prewarm_stderr_buffer += text

    def _on_prewarm_process_error(self, error: QProcess.ProcessError) -> None:
        if self._prewarm_process is None:
            return
        self._on_prewarm_failed(f"Preload process error: {int(error)}")
        self._on_prewarm_finished()

    def _on_prewarm_process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if exit_code != 0 and self._prewarm_stderr_buffer.strip():
            self._on_prewarm_failed(self._prewarm_stderr_buffer.strip().splitlines()[-1])
        self._on_prewarm_finished()

    def _on_prewarm_progress(self, stage: str, current: int, total: int) -> None:
        self._set_background_progress(stage, current, total)

    def _on_prewarm_completed(self, message: str) -> None:
        msg = message.strip() or "Preload complete"
        self._set_background_progress(msg, 1, 1)

    def _on_prewarm_failed(self, message: str) -> None:
        err = message.strip() or "Preload failed"
        self._set_background_progress(err, 1, 1)

    def _on_prewarm_finished(self) -> None:
        if self._prewarm_process is not None:
            self._prewarm_process.deleteLater()
        self._prewarm_process = None
        self._prewarm_in_progress = False
        self._request_gpu_status_update()
        QTimer.singleShot(2000, self._clear_background_progress)
