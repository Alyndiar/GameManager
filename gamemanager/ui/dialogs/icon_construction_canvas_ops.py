from __future__ import annotations

from .icon_construction_cutout_state import upsert_cutout_mark_point


class IconFramingCanvasOpsMixin:
    def _on_canvas_seed_color_picked(self, value: object) -> None:
        if self._active_seed_pick_index is None:
            return
        try:
            red = int(value[0])  # type: ignore[index]
            green = int(value[1])  # type: ignore[index]
            blue = int(value[2])  # type: ignore[index]
        except Exception:
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)
            self._refresh_seed_color_controls()
            self._refresh_cutout_status()
            return
        idx = self._active_seed_pick_index
        if 0 <= idx < len(self._seed_colors):
            self._seed_colors[idx] = (
                max(0, min(255, red)),
                max(0, min(255, green)),
                max(0, min(255, blue)),
            )
        self._active_seed_pick_index = None
        self._canvas.set_seed_pick_mode(False)
        self._refresh_seed_color_controls()
        self._apply_processing_settings()

    def _on_canvas_cutout_mark_point(self, value: object) -> None:
        if self._cutout_mark_mode not in {"add", "remove"}:
            return
        entry = self._active_cutout_mark_entry()
        if entry is None or str(entry.get("scope", "global")) != "contig":
            return
        try:
            x_val = float(value[0])  # type: ignore[index]
            y_val = float(value[1])  # type: ignore[index]
        except Exception:
            return
        point = (max(0.0, min(1.0, x_val)), max(0.0, min(1.0, y_val)))
        row_id = int(entry.get("id", -1))
        before = self._cutout_mark_snapshot(entry)
        if self._cutout_mark_mode == "add":
            include_points = entry.setdefault("include_seeds", [])
            if isinstance(include_points, list):
                upsert_cutout_mark_point(include_points, point)
        else:
            exclude_points = entry.setdefault("exclude_seeds", [])
            if isinstance(exclude_points, list):
                upsert_cutout_mark_point(exclude_points, point)
        after = self._cutout_mark_snapshot(entry)
        if after != before:
            self._push_cutout_mark_undo_snapshot(row_id, before)
        self._rebuild_cutout_pick_color_rows()
        self._update_cutout_mark_controls()
        self._apply_processing_settings()

    def _on_canvas_cutout_color_picked(self, value: object) -> None:
        try:
            red = max(0, min(255, int(value[0])))  # type: ignore[index]
            green = max(0, min(255, int(value[1])))  # type: ignore[index]
            blue = max(0, min(255, int(value[2])))  # type: ignore[index]
        except Exception:
            self._set_cutout_color_pick_mode(False)
            return
        self._set_cutout_color_pick_mode(False)
        row_id = self._cutout_row_uid_counter
        self._cutout_row_uid_counter += 1
        entry = {
            "id": row_id,
            "color": [red, green, blue],
            "tolerance": 10,
            "scope": "global",
            "falloff": "flat",
            "include_seeds": [],
            "exclude_seeds": [],
        }
        if entry not in self._cutout_picked_colors:
            self._cutout_picked_colors.append(entry)
            self._cutout_mark_history[row_id] = {"undo": [], "redo": []}
        self._sync_cutout_falloff_controls()
        self._rebuild_cutout_pick_color_rows()
        self._apply_processing_settings()

    def _on_canvas_manual_text_mark_point(self, value: object) -> None:
        if self._manual_text_mark_mode not in {"add", "remove"}:
            return
        try:
            x_val = float(value[0])  # type: ignore[index]
            y_val = float(value[1])  # type: ignore[index]
        except Exception:
            return
        point = (max(0.0, min(1.0, x_val)), max(0.0, min(1.0, y_val)))
        before = self._manual_points_snapshot()
        if self._manual_text_mark_mode == "add":
            self._upsert_manual_mark_point(self._manual_add_points, point)
        else:
            self._upsert_manual_mark_point(self._manual_remove_points, point)
        after = self._manual_points_snapshot()
        if after != before:
            self._manual_undo_stack.append(before)
            if len(self._manual_undo_stack) > self._manual_history_limit:
                del self._manual_undo_stack[0]
            self._manual_redo_stack.clear()
            self._update_manual_history_buttons()
        self._canvas.set_manual_text_points(self._manual_add_points, self._manual_remove_points)
        self._refresh_manual_mark_count_label()
        self._apply_processing_settings()

    def _on_canvas_roi_changed(self, roi_value: object) -> None:
        roi: list[float] | None = None
        if isinstance(roi_value, (list, tuple)) and len(roi_value) >= 4:
            try:
                roi = [
                    float(roi_value[0]),
                    float(roi_value[1]),
                    float(roi_value[2]),
                    float(roi_value[3]),
                ]
            except (TypeError, ValueError):
                roi = None
        self._text_roi = roi
        self._update_roi_label()
        self._apply_processing_settings()

