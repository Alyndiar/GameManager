from __future__ import annotations

from collections import Counter
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QListWidgetItem, QTableWidgetItem, QVBoxLayout, QWidget

from gamemanager.models import InventoryItem, RootDisplayInfo
from gamemanager.services.icon_cache import icon_cache_key
from gamemanager.services.sorting import natural_key
from gamemanager.services.storage import mountpoint_sort_key


class MainWindowInventoryOpsMixin:
    @staticmethod
    def _source_display_text(mode: str, source_label: str, drive_name: str) -> str:
        source = source_label.strip()
        drive = drive_name.strip()
        if mode == "source":
            return source
        if mode == "name":
            return drive
        if not source:
            return drive
        if not drive:
            return source
        if source.casefold() == drive.casefold():
            return source
        return f"{source} | {drive}"

    @staticmethod
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

    @staticmethod
    def _format_size_and_free(total_size_bytes: int, free_size_bytes: int) -> str:
        total_gb_rounded = int((total_size_bytes / (1024**3)) + 0.5)
        free_gb = free_size_bytes / (1024**3)
        return f"Size : {total_gb_rounded} GB    Free : {free_gb:.2f} GB"

    @staticmethod
    def _filter_only_duplicate_cleaned_names(items: list[InventoryItem]) -> list[InventoryItem]:
        counts = Counter(item.cleaned_name.strip().casefold() for item in items)
        return [
            item
            for item in items
            if counts[item.cleaned_name.strip().casefold()] > 1
        ]

    @staticmethod
    def _filter_by_root_id(
        items: list[InventoryItem], selected_root_id: int | None
    ) -> list[InventoryItem]:
        if selected_root_id is None:
            return items
        return [item for item in items if item.root_id == selected_root_id]

    @staticmethod
    def _filter_by_cleaned_name_query(
        items: list[InventoryItem], query_text: str
    ) -> list[InventoryItem]:
        needle = query_text.strip().casefold()
        if not needle:
            return items
        return [item for item in items if needle in item.cleaned_name.casefold()]

    def _build_sort_chain(self, primary_field: str, primary_ascending: bool) -> list[tuple[str, bool]]:
        ordered = [(primary_field, primary_ascending)] + list(self._default_right_sort_chain)
        seen: set[str] = set()
        chain: list[tuple[str, bool]] = []
        for field, asc in ordered:
            if field in seen:
                continue
            seen.add(field)
            chain.append((field, asc))
        return chain

    def _selected_root_id(self) -> int | None:
        rows = self.left_table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        item = self.left_table.item(row, 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _sorted_roots(self, roots: list[RootDisplayInfo]) -> list[RootDisplayInfo]:
        sort_key = self._left_sort_options[self.left_sort_combo.currentText()]
        if sort_key == "source_label":
            return sorted(
                roots,
                key=lambda x: (mountpoint_sort_key(x.mountpoint), x.source_label.casefold()),
            )
        if sort_key == "drive_name":
            return sorted(
                roots,
                key=lambda x: (mountpoint_sort_key(x.mountpoint), x.drive_name.casefold()),
            )
        return sorted(roots, key=lambda x: x.free_space_bytes, reverse=True)

    def _root_text(self, info: RootDisplayInfo) -> str:
        mode = self._left_label_options[self.left_label_combo.currentText()]
        return self._source_display_text(mode, info.source_label, info.drive_name)

    def _source_for_item(
        self, entry: InventoryItem, root_info_by_id: dict[int, RootDisplayInfo]
    ) -> str:
        mode = self._left_label_options[self.left_label_combo.currentText()]
        info = root_info_by_id.get(entry.root_id)
        if info is None:
            return entry.source_label
        return self._source_display_text(mode, info.source_label, info.drive_name)

    def _sort_value_for_field(
        self,
        entry: InventoryItem,
        field: str,
        root_info_by_id: dict[int, RootDisplayInfo],
    ):
        if field == "full_name":
            return natural_key(entry.full_name)
        if field == "cleaned_name":
            return natural_key(entry.cleaned_name)
        if field == "created_at":
            return entry.created_at
        if field == "modified_at":
            return entry.modified_at
        if field == "size_bytes":
            return entry.size_bytes
        if field == "source":
            return natural_key(self._source_for_item(entry, root_info_by_id))
        return natural_key(entry.full_name)

    def _sorted_inventory(
        self, items: list[InventoryItem], root_info_by_id: dict[int, RootDisplayInfo]
    ) -> list[InventoryItem]:
        primary_field = self._right_columns[self._right_sort_column][0]
        chain = self._build_sort_chain(primary_field, self._right_sort_ascending)
        ordered = list(items)
        for field, asc in reversed(chain):
            key_func = (
                lambda item, f=field: self._sort_value_for_field(item, f, root_info_by_id)
            )
            ordered.sort(key=key_func, reverse=not asc)
        return ordered

    def _build_root_cell_widget(self, info: RootDisplayInfo) -> QWidget:
        container = QFrame(self.left_table)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        top_line = QHBoxLayout()
        top_line.setContentsMargins(0, 0, 0, 0)
        top_line.setSpacing(8)

        left_label = QLabel(self._root_text(info), container)
        left_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        free_label = QLabel(
            self._format_size_and_free(info.total_size_bytes, info.free_space_bytes),
            container,
        )
        free_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        top_line.addWidget(left_label, 1)
        top_line.addWidget(free_label, 0)
        layout.addLayout(top_line)

        path_label = QLabel(info.root_path, container)
        path_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(path_label)
        return container

    def _populate_left(self, roots: list[RootDisplayInfo]) -> None:
        selected_root_id = self._selected_root_id()
        ordered = self._sorted_roots(roots)
        self.left_table.setUpdatesEnabled(False)
        try:
            self.left_table.setRowCount(len(ordered))
            for row, info in enumerate(ordered):
                item = QTableWidgetItem("")
                item.setData(Qt.ItemDataRole.UserRole, info.root_id)
                item.setToolTip(
                    f"Source: {info.source_label}\nDrive: {info.drive_name}\nRoot: {info.root_path}\nMountpoint: {info.mountpoint}"
                )
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                self.left_table.setItem(row, 0, item)
                self.left_table.setCellWidget(row, 0, self._build_root_cell_widget(info))
                if selected_root_id is not None and info.root_id == selected_root_id:
                    self.left_table.selectRow(row)
            self.left_table.resizeRowsToContents()
        finally:
            self.left_table.setUpdatesEnabled(True)

    def _populate_right(self, items: list[InventoryItem]) -> None:
        root_info_by_id = {info.root_id: info for info in self.root_infos}
        selected_root_id = self._selected_root_id()
        visible_items = self._filter_by_root_id(items, selected_root_id)
        visible_items = self._filter_by_cleaned_name_query(
            visible_items, self.left_filter_edit.text()
        )
        visible_items = (
            self._filter_only_duplicate_cleaned_names(visible_items)
            if self._show_only_duplicates
            else visible_items
        )
        sorted_items = self._sorted_inventory(visible_items, root_info_by_id)
        icon_px = self._icon_view_sizes[self._right_icon_size_index]
        self._visible_right_items = sorted_items
        self.right_table.setUpdatesEnabled(False)
        self.right_icon_list.setUpdatesEnabled(False)
        try:
            self.right_table.setRowCount(len(sorted_items))
            self.right_icon_list.clear()
            for row, entry in enumerate(sorted_items):
                row_icon = self._icon_for_entry(entry)
                row_source = self._source_for_item(entry, root_info_by_id)
                name_item = QTableWidgetItem(entry.full_name)
                name_item.setIcon(row_icon)
                self.right_table.setItem(row, 0, name_item)
                self.right_table.setItem(row, 1, QTableWidgetItem(entry.cleaned_name))
                self.right_table.setItem(
                    row, 2, QTableWidgetItem(entry.modified_at.strftime("%Y-%m-%d %H:%M:%S"))
                )
                self.right_table.setItem(
                    row, 3, QTableWidgetItem(entry.created_at.strftime("%Y-%m-%d %H:%M:%S"))
                )
                self.right_table.setItem(row, 4, QTableWidgetItem(self._format_bytes(entry.size_bytes)))
                self.right_table.setItem(
                    row, 5, QTableWidgetItem(row_source)
                )
                tooltip = self._entry_tooltip_text(entry, row_source)
                for col in range(len(self._right_columns)):
                    cell = self.right_table.item(row, col)
                    if cell is not None:
                        cell.setToolTip(tooltip)

                tile_text = entry.cleaned_name.strip() or entry.full_name
                tile_icon = row_icon
                if tile_icon.cacheKey() == self._blank_icon.cacheKey():
                    tile_icon = self._icon_placeholder(icon_px)
                tile = QListWidgetItem(tile_icon, tile_text)
                tile.setToolTip(tooltip)
                tile.setData(Qt.ItemDataRole.UserRole, row)
                tile.setTextAlignment(
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                )
                self.right_icon_list.addItem(tile)

            self.right_table.resizeColumnsToContents()
        finally:
            self.right_table.setUpdatesEnabled(True)
            self.right_icon_list.setUpdatesEnabled(True)
        self._apply_right_icon_size_ui()
        self._update_counts_status()

    def _restore_right_icon_selection(
        self,
        selected_paths: list[str],
        anchor_path: str | None = None,
    ) -> None:
        if self._right_view_mode != "icons" or not selected_paths:
            return
        path_to_rows: dict[str, list[int]] = {}
        for row, entry in enumerate(self._visible_right_items):
            path_to_rows.setdefault(entry.full_path, []).append(row)
        rows_to_select: list[int] = []
        for path in selected_paths:
            rows_to_select.extend(path_to_rows.get(path, []))
        if not rows_to_select:
            return
        rows_to_select = sorted(set(rows_to_select))
        blocked = self.right_icon_list.blockSignals(True)
        try:
            self.right_icon_list.clearSelection()
            for row in rows_to_select:
                item = self.right_icon_list.item(row)
                if item is not None:
                    item.setSelected(True)
            anchor_row: int | None = None
            if anchor_path:
                anchor_rows = path_to_rows.get(anchor_path, [])
                if anchor_rows:
                    anchor_row = anchor_rows[0]
            if anchor_row is None:
                anchor_row = rows_to_select[0]
            anchor_item = self.right_icon_list.item(anchor_row)
            if anchor_item is not None:
                self.right_icon_list.setCurrentItem(anchor_item)
                self.right_icon_list.scrollToItem(anchor_item)
        finally:
            self.right_icon_list.blockSignals(blocked)
        self._update_counts_status()

    def _prune_icon_caches(self) -> None:
        valid_icon_keys: set[str] = set()
        valid_preview_keys: set[str] = set()
        for entry in self.inventory:
            if not (entry.is_dir and entry.icon_status == "valid" and entry.folder_icon_path):
                continue
            icon_path = os.path.normpath(entry.folder_icon_path)
            valid_icon_keys.add(icon_cache_key(icon_path, 16))
            valid_preview_keys.add(icon_cache_key(icon_path, 256))
        self._folder_icon_cache = {
            key: icon for key, icon in self._folder_icon_cache.items() if key in valid_icon_keys
        }
        self._folder_icon_preview_cache = {
            key: pix
            for key, pix in self._folder_icon_preview_cache.items()
            if key in valid_preview_keys
        }

