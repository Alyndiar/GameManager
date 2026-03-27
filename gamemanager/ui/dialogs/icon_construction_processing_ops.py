from __future__ import annotations

from PySide6.QtCore import QThread, QTimer

from .icon_construction_workers import FramingProcessingWorker


class IconFramingProcessingOpsMixin:
    def _set_processing_status(self, text: str) -> None:
        self.processing_status_label.setText(text.strip() or "Ready.")

    def _set_processing_controls_busy(self, busy: bool) -> None:
        self.border_combo.setEnabled(not busy)
        self.zoom_spin.setEnabled(not busy)
        self.zoom_out_btn.setEnabled(not busy)
        self.zoom_in_btn.setEnabled(not busy)
        self.reset_btn.setEnabled(not busy)
        self.upscale_method_combo.setEnabled(not busy)
        self.bg_removal_combo.setEnabled(not busy and self._template_enabled())
        self.text_method_combo.setEnabled(not busy and self._template_enabled())
        self.layer_all_btn.setEnabled(not busy)
        self.layer_none_btn.setEnabled(not busy)
        self.roi_draw_btn.setEnabled(not busy and self._roi_method_enabled())
        self.roi_clear_btn.setEnabled(not busy and self._roi_method_enabled())
        pick_colors_enabled = (
            not busy
            and self._cutout_method_enabled()
            and self.selected_bg_removal_engine() == "pick_colors"
        )
        self.cutout_pick_add_btn.setEnabled(pick_colors_enabled)
        self.cutout_pick_clear_btn.setEnabled(
            pick_colors_enabled and bool(self._cutout_picked_colors)
        )
        self.cutout_falloff_advanced_check.setEnabled(pick_colors_enabled)
        for button in self._seed_swatch_buttons:
            button.setEnabled(not busy and self._text_method_enabled())
        for idx in range(self.cutout_pick_rows_layout.count()):
            item = self.cutout_pick_rows_layout.itemAt(idx)
            widget = item.widget()
            if widget is not None:
                widget.setEnabled(pick_colors_enabled)
        self.manual_mark_undo_btn.setEnabled(False)
        self.manual_mark_redo_btn.setEnabled(False)
        self.manual_mark_add_btn.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_remove_btn.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_stop_btn.setEnabled(not busy and self._text_method_enabled())
        if busy and self._canvas.cutout_color_pick_mode():
            self._set_cutout_color_pick_mode(False)
        if busy and self._cutout_mark_mode != "none":
            self._set_cutout_mark_mode("none")
        self._sync_cutout_falloff_controls()
        self._update_cutout_mark_controls()
        if not busy:
            self._update_manual_history_buttons()

    def _start_processing_worker(
        self,
        bg_engine: str,
        bg_params: dict[str, object],
        text_cfg: dict[str, object],
        *,
        text_debug_alpha: bool,
    ) -> None:
        include_cutout = bg_engine != "none"
        include_text = bool(text_cfg.get("enabled", False)) and (
            str(text_cfg.get("method", "none")) != "none"
        )
        if self._processing_in_progress:
            self._pending_processing = True
            self._set_processing_status("Queued settings update...")
            return
        if not include_cutout and not include_text:
            self._canvas.set_bg_removal_engine(bg_engine)
            self._canvas.set_bg_removal_params(bg_params)
            self._canvas.set_text_preserve_config(text_cfg)
            self._refresh_cutout_status()
            self._set_processing_status("Ready.")
            return

        self._processing_in_progress = True
        self._pending_processing = False
        self._canvas.set_async_processing_busy(True)
        self._set_processing_controls_busy(True)
        self._set_processing_status("Preparing layers...")
        thread = QThread(self)
        worker = FramingProcessingWorker(
            source_image_bytes=self._canvas.source_image_bytes(),
            bg_engine=bg_engine,
            bg_params=bg_params,
            text_config=text_cfg,
            include_cutout=include_cutout,
            include_text_overlay=include_text,
            include_text_alpha=include_text and text_debug_alpha,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_processing_progress)
        worker.completed.connect(
            lambda payload, be=bg_engine, bp=bg_params, tc=text_cfg: self._on_processing_completed(
                payload, be, bp, tc
            )
        )
        worker.failed.connect(self._on_processing_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_processing_finished)
        self._processing_thread = thread
        self._processing_worker = worker
        thread.start()

    def _on_processing_progress(self, stage: str, current: int, total: int) -> None:
        current_i = max(0, int(current))
        total_i = max(0, int(total))
        if total_i > 0:
            current_i = min(current_i, total_i)
            pct = int(round((current_i / total_i) * 100.0))
            self._set_processing_status(f"{stage}: {current_i}/{total_i} ({pct}%)")
            return
        self._set_processing_status(stage)

    def _on_processing_completed(
        self,
        payload_obj: object,
        bg_engine: str,
        bg_params: dict[str, object],
        text_cfg: dict[str, object],
    ) -> None:
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        self._canvas.set_bg_removal_engine(bg_engine)
        self._canvas.set_bg_removal_params(bg_params)
        self._canvas.set_text_preserve_config(text_cfg)

        cutout_key = self._canvas.build_cutout_cache_key(bg_engine, bg_params)
        self._canvas.store_cutout_payload(
            cutout_key,
            payload.get("cutout_bytes")
            if isinstance(payload.get("cutout_bytes"), (bytes, bytearray))
            else None,
            error=str(payload.get("cutout_error") or "").strip() or None,
        )

        text_key = self._canvas.build_text_overlay_cache_key(bg_engine, bg_params, text_cfg)
        text_payload = payload.get("text_overlay_bytes")
        self._canvas.store_text_overlay_payload(
            text_key,
            bytes(text_payload) if isinstance(text_payload, (bytes, bytearray)) else None,
        )

        text_alpha_key = self._canvas.build_text_alpha_cache_key(bg_engine, bg_params, text_cfg)
        text_alpha_payload = payload.get("text_alpha_bytes")
        self._canvas.store_text_alpha_payload(
            text_alpha_key,
            bytes(text_alpha_payload)
            if isinstance(text_alpha_payload, (bytes, bytearray))
            else None,
        )
        self._canvas.update()
        self._refresh_cutout_status()
        self._set_processing_status("Ready.")

    def _on_processing_failed(self, message: str) -> None:
        self._canvas.set_bg_removal_engine(self.selected_bg_removal_engine())
        self._canvas.set_bg_removal_params(self.selected_bg_removal_params())
        self._canvas.set_text_preserve_config(self.selected_text_preserve_config())
        self._canvas.update()
        self._set_processing_status(f"Processing failed: {message.strip() or 'Unknown error'}")
        self._refresh_cutout_status()

    def _on_processing_finished(self) -> None:
        self._processing_thread = None
        self._processing_worker = None
        self._processing_in_progress = False
        self._canvas.set_async_processing_busy(False)
        self._set_processing_controls_busy(False)
        if self._pending_processing:
            self._pending_processing = False
            self._apply_processing_settings()
            return
        if self._apply_after_processing:
            self._apply_after_processing = False
            QTimer.singleShot(0, self._on_apply)

    def _apply_processing_settings(self) -> None:
        if self._spinner_apply_timer.isActive():
            self._spinner_apply_timer.stop()
        self._start_processing_worker(
            self.selected_bg_removal_engine(),
            self.selected_bg_removal_params(),
            self.selected_text_preserve_config(),
            text_debug_alpha=self.debug_text_alpha_check.isChecked(),
        )

    def _shutdown_processing_thread(
        self,
        *,
        timeout_ms: int = 2500,
        allow_terminate: bool = False,
    ) -> None:
        thread = self._processing_thread
        if thread is None:
            return
        try:
            thread.requestInterruption()
        except Exception:
            pass
        try:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(max(100, int(timeout_ms))) and allow_terminate:
                    thread.terminate()
                    thread.wait(max(100, int(timeout_ms // 2)))
        except Exception:
            pass
        self._processing_thread = None
        self._processing_worker = None
        self._processing_in_progress = False
        self._pending_processing = False
        self._apply_after_processing = False
        self._canvas.set_async_processing_busy(False)

