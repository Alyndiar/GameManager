from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import TypeVar, cast

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from gamemanager.models import OperationReport

_T = TypeVar("_T")


class MainWindowOperationOpsMixin:
    def _set_background_operation_busy(self, busy: bool) -> None:
        self.cleanup_btn.setEnabled(not busy)
        self.move_btn.setEnabled(not busy)
        self.add_root_btn.setEnabled(not busy)
        self.remove_root_btn.setEnabled(not busy)
        self.tags_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled((not busy) and (not self._refresh_in_progress))
        self._update_cancel_button_state()

    def _start_report_operation(
        self,
        title: str,
        run_fn: Callable[
            [Callable[[str, int, int], None], Callable[[], bool]],
            OperationReport,
        ],
        on_complete: Callable[[OperationReport], None],
    ) -> bool:
        if self._operation_in_progress:
            QMessageBox.information(
                self,
                "Operation In Progress",
                "Wait for the current operation to finish first.",
            )
            return False
        if self._refresh_in_progress:
            QMessageBox.information(
                self,
                "Refresh In Progress",
                "Wait for the current refresh to finish first.",
            )
            return False
        if self._teracopy_processes:
            QMessageBox.information(
                self,
                "Move In Progress",
                "Wait for the current TeraCopy operation to finish first.",
            )
            return False

        self._operation_in_progress = True
        self._operation_title = title.strip() or "Operation"
        self._operation_complete_handler = on_complete
        self._set_background_operation_busy(True)
        self._set_operation_progress(self._operation_title, 0, 1)

        thread = QThread(self)
        worker = self._report_worker_cls(run_fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_operation_progress)
        worker.completed.connect(self._on_operation_completed)
        worker.canceled.connect(self._on_operation_canceled)
        worker.failed.connect(self._on_operation_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_operation_finished)
        self._operation_thread = thread
        self._operation_worker = worker
        thread.start()
        return True

    def _on_operation_progress(self, stage: str, current: int, total: int) -> None:
        label = stage.strip() or self._operation_title or "Operation"
        self._set_operation_progress(label, current, total)

    def _on_operation_completed(self, report_obj: object) -> None:
        report = report_obj if isinstance(report_obj, OperationReport) else OperationReport()
        handler = self._operation_complete_handler
        if handler is not None:
            handler(report)
        self._set_operation_progress(self._operation_title or "Operation complete", 1, 1)

    def _on_operation_failed(self, message: str) -> None:
        err = message.strip() or "Unknown operation error."
        QMessageBox.warning(self, "Operation Failed", err)
        self._set_operation_progress(
            f"{self._operation_title or 'Operation'} failed", 1, 1
        )

    def _on_operation_canceled(self, message: str) -> None:
        msg = message.strip() or f"{self._operation_title or 'Operation'} canceled"
        self._set_operation_progress(msg, 1, 1)

    def _on_operation_finished(self) -> None:
        self._operation_thread = None
        self._operation_worker = None
        self._operation_complete_handler = None
        self._operation_in_progress = False
        self._operation_title = ""
        self._set_background_operation_busy(False)
        if (
            not self._refresh_in_progress
            and not self._interactive_operation_active
            and not self._teracopy_processes
        ):
            self._clear_operation_progress()

    def _update_cancel_button_state(self) -> None:
        can_cancel = (
            self._refresh_in_progress
            or self._operation_in_progress
            or self._interactive_operation_active
            or bool(self._teracopy_processes)
        )
        self.cancel_op_btn.setEnabled(can_cancel)

    def _on_cancel_operation(self) -> None:
        if self._refresh_in_progress and self._refresh_worker is not None:
            self._refresh_worker.request_cancel()
            self._set_operation_progress("Canceling refresh", 0, 1)
            self._update_cancel_button_state()
            return
        if self._operation_in_progress and self._operation_worker is not None:
            self._operation_worker.request_cancel()
            self._set_operation_progress("Canceling operation", 0, 1)
            self._update_cancel_button_state()
            return
        if self._interactive_operation_active:
            self._interactive_cancel_requested = True
            self._set_operation_progress("Cancel requested", 0, 1)
            self._update_cancel_button_state()
            return
        if self._teracopy_processes:
            self._cancel_teracopy_session()
            self._update_cancel_button_state()

    def _begin_interactive_operation(self, title: str, total: int) -> None:
        self._interactive_operation_active = True
        self._interactive_cancel_requested = False
        self._set_operation_progress(title, 0, max(1, total))
        self._update_cancel_button_state()

    def _step_interactive_operation(self, title: str, current: int, total: int) -> bool:
        self._set_operation_progress(title, current, max(1, total))
        QApplication.processEvents()
        return self._interactive_cancel_requested

    def _end_interactive_operation(self) -> None:
        self._interactive_operation_active = False
        self._interactive_cancel_requested = False
        self._update_cancel_button_state()
        if (
            not self._refresh_in_progress
            and not self._operation_in_progress
            and not self._teracopy_processes
        ):
            self._clear_operation_progress()

    def _run_ui_pumped_call(
        self,
        stage: str,
        fn: Callable[[], _T],
    ) -> _T:
        state: dict[str, object] = {
            "done": False,
            "value": None,
            "error": None,
        }

        def _worker() -> None:
            try:
                state["value"] = fn()
            except Exception as exc:
                state["error"] = exc
            finally:
                state["done"] = True

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        while not bool(state["done"]):
            self._set_operation_progress(stage, 0, 0)
            QApplication.processEvents()
            time.sleep(0.02)
        err = state.get("error")
        if isinstance(err, Exception):
            raise err
        if (
            not self._refresh_in_progress
            and not self._operation_in_progress
            and not self._interactive_operation_active
            and not self._teracopy_processes
        ):
            self._clear_operation_progress()
        return cast(_T, state.get("value"))

