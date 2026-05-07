from __future__ import annotations

from collections.abc import Callable
import os
import shutil
import tempfile

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QFileDialog, QMessageBox

from gamemanager.models import MovePlanItem, OperationReport
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.teracopy import resolve_teracopy_path


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class MainWindowTransferOpsMixin:
    def _move_selected_entries_to_root(self, destination_root_id: int) -> None:
        selected = self._selected_right_entries()
        if not selected:
            return
        destination = next(
            (info for info in self.root_infos if info.root_id == destination_root_id), None
        )
        if destination is None:
            QMessageBox.warning(self, "Invalid Destination", "Destination root not found.")
            return

        total_size = sum(item.size_bytes for item in selected)
        try:
            free_space = shutil.disk_usage(destination.root_path).free
        except OSError:
            free_space = destination.free_space_bytes
        if total_size > free_space:
            QMessageBox.warning(
                self,
                "Not Enough Space",
                "Drag and drop cancelled: not enough free space on destination root.\n\n"
                f"Selected size: {_format_bytes(total_size)}\n"
                f"Free on destination: {_format_bytes(free_space)}\n"
                f"Destination root: {destination.root_path}",
            )
            return

        conflicts: list[str] = []
        move_pairs: list[tuple[str, str]] = []
        for entry in selected:
            src = os.path.normpath(entry.full_path)
            dst = os.path.normpath(os.path.join(destination.root_path, entry.full_name))
            if os.path.normcase(src) == os.path.normcase(dst):
                continue
            if os.path.exists(dst):
                conflicts.append(f"{entry.full_name} -> {dst}")
                continue
            move_pairs.append((src, dst))
        if conflicts:
            details = "\n".join(conflicts[:8])
            QMessageBox.warning(
                self,
                "Destination Conflicts",
                "Drag and drop cancelled: destination already has item(s).\n\n"
                f"{details}",
            )
            return
        if not move_pairs:
            return

        answer = QMessageBox.question(
            self,
            "Confirm Move",
            "Move selected games to destination root?\n\n"
            f"Games selected: {len(move_pairs)}\n"
            f"Total size: {_format_bytes(total_size)}\n"
            f"Destination root: {destination.root_path}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return

        if self._current_move_backend() == "teracopy":
            if self._teracopy_processes:
                QMessageBox.information(
                    self,
                    "Move In Progress",
                    "Wait for the current TeraCopy operation to finish first.",
                )
                return
            if self._start_teracopy_move_pairs(
                move_pairs, completion_title="Move Completed"
            ):
                return
            QMessageBox.warning(
                self,
                "TeraCopy Unavailable",
                "TeraCopy could not be located. Falling back to system move.",
            )

        def _run(progress_cb, should_cancel):
            report = OperationReport(total=len(move_pairs))
            total = len(move_pairs)
            if progress_cb is not None:
                progress_cb("Move selected games", 0, total)
            for idx, (src, dst) in enumerate(move_pairs, start=1):
                if should_cancel():
                    raise OperationCancelled("Move selected games canceled")
                try:
                    shutil.move(src, dst)
                    report.succeeded += 1
                except OSError as exc:
                    report.failed += 1
                    report.details.append(f"{src} -> {dst}: {exc}")
                if progress_cb is not None:
                    progress_cb("Move selected games", idx, total)
            return report

        def _done(report: OperationReport) -> None:
            self.refresh_all()
            if report.failed:
                details = "\n".join(report.details[:8])
                QMessageBox.warning(
                    self,
                    "Move Completed with Errors",
                    f"Moved: {report.succeeded}\nFailed: {report.failed}\n\n{details}",
                )
                return
            self._show_success_popup("Move Completed", f"Moved: {report.succeeded}")

        self._start_report_operation("Move selected games", _run, _done)

    def _set_move_controls_busy(self, busy: bool) -> None:
        self.move_btn.setEnabled(not busy)
        self.move_backend_combo.setEnabled(not busy)
        self.locate_teracopy_btn.setEnabled(
            (not busy) and self._current_move_backend() == "teracopy"
        )
        self.right_table.setDragEnabled(not busy)
        self.right_icon_list.setDragEnabled(not busy)
        self._update_cancel_button_state()

    def _resolve_teracopy_for_move(self, allow_manual_pick: bool) -> str | None:
        resolved = resolve_teracopy_path(self._teracopy_path_pref)
        if resolved:
            if resolved != self._teracopy_path_pref:
                self._teracopy_path_pref = resolved
                self.state.set_ui_pref("teracopy_path", resolved)
            self._teracopy_executable = resolved
            self._update_move_backend_ui()
            return resolved
        if not allow_manual_pick:
            return None
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Locate TeraCopy.exe",
            r"C:\Program Files\TeraCopy",
            "Executables (*.exe);;All Files (*)",
        )
        if not selected:
            return None
        selected = os.path.normpath(selected)
        if not os.path.isfile(selected):
            QMessageBox.warning(
                self, "Invalid TeraCopy Path", f"File does not exist:\n{selected}"
            )
            return None
        self._teracopy_path_pref = selected
        self._teracopy_executable = selected
        self.state.set_ui_pref("teracopy_path", selected)
        self._update_move_backend_ui()
        return selected

    def _start_teracopy_move_pairs(
        self,
        move_pairs: list[tuple[str, str]],
        completion_title: str,
        on_finish: Callable[[int, int, list[str]], None] | None = None,
    ) -> bool:
        if self._teracopy_processes:
            QMessageBox.information(
                self,
                "Move In Progress",
                "Wait for the current TeraCopy operation to finish first.",
            )
            return False

        teracopy_exe = self._resolve_teracopy_for_move(allow_manual_pick=True)
        if not teracopy_exe:
            return False

        grouped: dict[str, list[str]] = {}
        unsupported: list[tuple[str, str]] = []
        for src, dst in move_pairs:
            src_name = os.path.basename(src)
            dst_name = os.path.basename(dst)
            if src_name.casefold() != dst_name.casefold():
                unsupported.append((src, dst))
                continue
            target_dir = os.path.dirname(dst)
            grouped.setdefault(target_dir, []).append(src)

        batches: list[tuple[str, str, int]] = []
        self._teracopy_temp_files = []
        self._teracopy_total_items = 0
        self._teracopy_succeeded_items = 0
        self._teracopy_failed_items = len(unsupported)
        self._teracopy_completion_title = completion_title
        self._teracopy_finish_callback = on_finish
        self._teracopy_session_active = True
        self._teracopy_failure_details = [
            f"Fallback needed (renamed destination): {src} -> {dst}"
            for src, dst in unsupported
        ]
        self._teracopy_job_meta.clear()
        self._teracopy_job_output.clear()

        for target_dir, sources in grouped.items():
            if not sources:
                continue
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as exc:
                self._teracopy_failed_items += len(sources)
                self._teracopy_failure_details.append(
                    f"Cannot create target folder {target_dir}: {exc}"
                )
                continue
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", suffix=".txt", delete=False
            )
            with handle:
                for src in sources:
                    handle.write(f"{src}\n")
            list_path = os.path.normpath(handle.name)
            self._teracopy_temp_files.append(list_path)
            batches.append((list_path, target_dir, len(sources)))
            self._teracopy_total_items += len(sources)

        if not batches:
            self._cleanup_teracopy_temp_files()
            self._set_operation_progress("TeraCopy move", 0, 0)
            self.refresh_all()
            if self._teracopy_finish_callback is not None:
                callback = self._teracopy_finish_callback
                self._teracopy_finish_callback = None
                callback(0, self._teracopy_failed_items, self._teracopy_failure_details)
            elif self._teracopy_failed_items:
                details = "\n".join(self._teracopy_failure_details[:8])
                QMessageBox.warning(
                    self,
                    completion_title,
                    f"Moved: 0\nFailed: {self._teracopy_failed_items}\n\n{details}",
                )
            return True

        self._set_move_controls_busy(True)
        self._teracopy_executable = teracopy_exe
        self._set_operation_progress("TeraCopy move", 0, self._teracopy_total_items)
        for list_path, target_dir, batch_size in batches:
            self._start_teracopy_batch(list_path, target_dir, batch_size)
        return True

    def _start_teracopy_batch(self, list_path: str, target_dir: str, batch_size: int) -> None:
        proc = QProcess(self)
        pid = id(proc)
        self._teracopy_processes[pid] = proc
        self._teracopy_job_meta[pid] = (batch_size, target_dir)
        self._teracopy_job_output[pid] = ""
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_teracopy_ready_read(p))
        proc.readyReadStandardError.connect(lambda p=proc: self._on_teracopy_ready_read(p))
        proc.errorOccurred.connect(lambda err, p=proc: self._on_teracopy_error(p, err))
        proc.finished.connect(
            lambda code, status, p=proc: self._on_teracopy_finished(p, code, status)
        )
        proc.setProgram(self._teracopy_executable or "")
        proc.setArguments(["Move", f"*{list_path}", target_dir, "/Close"])
        proc.start()
        self._update_cancel_button_state()

    def _on_teracopy_ready_read(self, proc: QProcess) -> None:
        pid = id(proc)
        if pid not in self._teracopy_processes:
            return
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="ignore")
        err = bytes(proc.readAllStandardError()).decode("utf-8", errors="ignore")
        chunk = (out + "\n" + err).strip()
        if chunk:
            previous = self._teracopy_job_output.get(pid, "")
            self._teracopy_job_output[pid] = f"{previous}\n{chunk}".strip()

    def _on_teracopy_error(self, proc: QProcess, process_error: QProcess.ProcessError) -> None:
        pid = id(proc)
        _batch_size, target = self._teracopy_job_meta.get(pid, (0, "?"))
        self._teracopy_failure_details.append(
            f"TeraCopy process error {int(process_error)} for {target}"
        )

    def _on_teracopy_finished(
        self, proc: QProcess, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        pid = id(proc)
        batch_size, target = self._teracopy_job_meta.get(pid, (0, "?"))
        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self._teracopy_succeeded_items += batch_size
        else:
            self._teracopy_failed_items += batch_size
            details = self._teracopy_job_output.get(pid, "")
            if details:
                details = details.splitlines()[-1].strip()
            if not details:
                details = f"exit_code={exit_code}"
            self._teracopy_failure_details.append(
                f"TeraCopy failed for {target}: {details}"
            )
        self._teracopy_job_output.pop(pid, None)
        self._teracopy_job_meta.pop(pid, None)
        self._teracopy_processes.pop(pid, None)
        proc.deleteLater()
        self._update_cancel_button_state()

        done = self._teracopy_succeeded_items + self._teracopy_failed_items
        self._set_operation_progress(
            "TeraCopy move",
            done,
            max(1, self._teracopy_total_items),
        )
        if self._teracopy_session_active and not self._teracopy_processes:
            self._finish_teracopy_session()

    def _cleanup_teracopy_temp_files(self) -> None:
        for path in self._teracopy_temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._teracopy_temp_files.clear()

    def _finish_teracopy_session(self) -> None:
        if not self._teracopy_session_active:
            return
        self._teracopy_session_active = False
        self._cleanup_teracopy_temp_files()
        self._set_move_controls_busy(False)
        succeeded = self._teracopy_succeeded_items
        failed = self._teracopy_failed_items
        details = list(self._teracopy_failure_details)
        callback = self._teracopy_finish_callback
        self._teracopy_finish_callback = None
        done = succeeded + failed
        self._set_operation_progress(
            "TeraCopy move",
            done,
            max(1, self._teracopy_total_items),
        )
        self.refresh_all()
        if callback is not None:
            callback(succeeded, failed, details)
            return
        if failed:
            detail_text = "\n".join(details[:8])
            QMessageBox.warning(
                self,
                self._teracopy_completion_title,
                f"Moved: {succeeded}\nFailed: {failed}\n\n{detail_text}",
            )
            return
        self._show_success_popup(self._teracopy_completion_title, f"Moved: {succeeded}")

    def _cancel_teracopy_session(self) -> None:
        if not self._teracopy_session_active:
            return
        processes = list(self._teracopy_processes.values())
        for proc in processes:
            try:
                proc.kill()
            except Exception:
                continue
        # Count any still-running jobs as failed; finished handlers will remove maps.
        remaining_items = sum(batch for batch, _target in self._teracopy_job_meta.values())
        if remaining_items > 0:
            self._teracopy_failed_items += remaining_items
            self._teracopy_failure_details.append("Canceled by user.")
        self._teracopy_job_meta.clear()
        self._teracopy_job_output.clear()
        self._teracopy_processes.clear()
        self._update_cancel_button_state()
        self._finish_teracopy_session()

    def _on_move_archives(self) -> None:
        plan = self.state.build_archive_move_plan({".iso", ".zip", ".rar", ".7z"})
        if not plan:
            QMessageBox.information(
                self, "Nothing to Move", "No root-level ISO/ZIP/RAR/7Z files found."
            )
            return
        dialog = self._move_preview_dialog_cls(plan, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        applied_items = dialog.applied_items()
        if self._current_move_backend() == "teracopy":
            self._execute_archive_moves_with_teracopy(applied_items)
            return

        def _run(progress_cb, should_cancel):
            return self.state.execute_archive_move_plan_with_progress(
                applied_items,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def _done(report: OperationReport) -> None:
            self._show_archive_move_report(report)
            self.refresh_all()

        self._start_report_operation("Move archives", _run, _done)

    def _show_archive_move_report(self, report: OperationReport) -> None:
        lines = [
            f"Attempted: {report.total}",
            f"Succeeded: {report.succeeded}",
            f"Skipped: {report.skipped}",
            f"Conflicts: {report.conflicts}",
            f"Failed: {report.failed}",
        ]
        if report.details:
            lines.append("")
            lines.extend(report.details[:8])
        has_issues = int(report.failed) > 0 or int(report.conflicts) > 0
        if has_issues:
            QMessageBox.warning(self, "Move Result", "\n".join(lines))
            return
        self._show_success_popup("Move Result", "\n".join(lines))

    def _execute_archive_moves_with_teracopy(
        self, plan_items: list[MovePlanItem]
    ) -> None:
        if self._teracopy_processes:
            QMessageBox.information(
                self,
                "Move In Progress",
                "Wait for the current TeraCopy operation to finish first.",
            )
            return
        if not self._resolve_teracopy_for_move(allow_manual_pick=True):
            QMessageBox.warning(
                self,
                "TeraCopy Unavailable",
                "TeraCopy could not be located. Falling back to system move.",
            )

            def _run(progress_cb, should_cancel):
                return self.state.execute_archive_move_plan_with_progress(
                    plan_items,
                    progress_cb=progress_cb,
                    should_cancel=should_cancel,
                )

            def _done(report: OperationReport) -> None:
                self._show_archive_move_report(report)
                self.refresh_all()

            self._start_report_operation("Move archives", _run, _done)
            return

        report = OperationReport(total=len(plan_items))
        teracopy_pairs: list[tuple[str, str]] = []

        for item in plan_items:
            action = item.selected_action
            if action == "skip":
                report.skipped += 1
                if item.status == "conflict":
                    report.conflicts += 1
                continue

            src_path = os.path.normpath(str(item.src_path))
            dst_path = os.path.normpath(str(item.dst_path))
            dst_folder = os.path.normpath(str(item.dst_folder))
            if action == "rename":
                if not item.manual_name:
                    report.failed += 1
                    report.details.append(
                        f"Missing manual name for {src_path}; action skipped."
                    )
                    continue
                dst_path = os.path.normpath(os.path.join(dst_folder, item.manual_name))

            if action in {"overwrite", "delete_destination"} and os.path.exists(dst_path):
                try:
                    self._delete_path(dst_path)
                except OSError as exc:
                    report.failed += 1
                    report.details.append(f"Failed removing destination {dst_path}: {exc}")
                    continue

            if os.path.exists(dst_path):
                report.conflicts += 1
                report.skipped += 1
                report.details.append(f"Conflict remains at destination: {dst_path}")
                continue
            if not os.path.exists(src_path):
                report.failed += 1
                report.details.append(f"Source does not exist: {src_path}")
                continue
            try:
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            except OSError as exc:
                report.failed += 1
                report.details.append(
                    f"Failed creating destination folder for {dst_path}: {exc}"
                )
                continue

            if os.path.basename(src_path).casefold() != os.path.basename(dst_path).casefold():
                try:
                    shutil.move(src_path, dst_path)
                    report.succeeded += 1
                except OSError as exc:
                    report.failed += 1
                    report.details.append(f"Failed move {src_path}: {exc}")
                continue
            teracopy_pairs.append((src_path, dst_path))

        def _on_finish(succeeded: int, failed: int, details: list[str]) -> None:
            report.succeeded += succeeded
            report.failed += failed
            report.details.extend(details)
            self._show_archive_move_report(report)

        if teracopy_pairs:
            if self._start_teracopy_move_pairs(
                teracopy_pairs,
                completion_title="Move Result",
                on_finish=_on_finish,
            ):
                return
            return

        self.refresh_all()
        self._show_archive_move_report(report)
