from __future__ import annotations

import os
import shutil
import subprocess
import sys
from urllib.parse import quote_plus
import webbrowser

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox

from gamemanager.models import InventoryItem
from gamemanager.services.normalization import cleaned_name_from_full
from gamemanager.services.storefronts.priority import normalize_store_name, sort_stores


class MainWindowActionsOpsMixin:
    def _on_right_item_double_clicked(self, table_item) -> None:
        row = table_item.row()
        if row < 0 or row >= len(self._visible_right_items):
            return
        entry = self._visible_right_items[row]
        self._open_in_explorer(entry.full_path)

    def _on_right_icon_item_double_clicked(self, list_item) -> None:
        row = self.right_icon_list.row(list_item)
        if row < 0 or row >= len(self._visible_right_items):
            return
        self.right_icon_list.clearSelection()
        list_item.setSelected(True)
        self._on_assign_folder_icon_selected()

    def _open_in_explorer(self, full_path: str) -> None:
        path = os.path.normpath(full_path)
        try:
            if os.path.isdir(path):
                subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["explorer", f"/select,{path}"])
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Cannot Open in Explorer",
                f"Could not open path:\n{path}\n\n{exc}",
            )

    def _on_right_context_menu(self, pos) -> None:
        index = self.right_table.indexAt(pos)
        if index.isValid() and index.row() >= 0 and not self.right_table.item(
            index.row(), 0
        ).isSelected():
            self.right_table.selectRow(index.row())

        menu = QMenu(self.right_table)
        open_action = QAction("Open Folder/Archive\tCtrl+O", menu)
        open_action.triggered.connect(self._on_open_selected_in_explorer)
        menu.addAction(open_action)
        menu.addSeparator()
        assign_icon_action = QAction("Assign Folder Icon...\tCtrl+I", menu)
        assign_icon_action.triggered.connect(self._on_assign_folder_icon_selected)
        menu.addAction(assign_icon_action)
        sgdb_status_action = QAction("SteamGridDB Status...", menu)
        sgdb_status_action.triggered.connect(self._on_steamgriddb_status_selected)
        menu.addAction(sgdb_status_action)
        sgdb_upload_action = QAction("Upload Icon to SteamGridDB...", menu)
        sgdb_upload_action.triggered.connect(self._on_upload_icon_to_steamgriddb_selected)
        menu.addAction(sgdb_upload_action)
        sgdb_upload_missing_action = QAction("Upload Missing Icons to SteamGridDB...", menu)
        sgdb_upload_missing_action.triggered.connect(self._on_upload_missing_icons_to_steamgriddb_selected)
        menu.addAction(sgdb_upload_missing_action)
        rebuild_icons_action = QAction("Rebuild Existing Icons\tCtrl+Shift+B", menu)
        rebuild_icons_action.triggered.connect(self._on_rebuild_existing_icons)
        menu.addAction(rebuild_icons_action)
        backfill_sources_action = QAction("Backfill Missing Icon Sources\tCtrl+Shift+U", menu)
        backfill_sources_action.triggered.connect(self._on_backfill_missing_icon_sources)
        menu.addAction(backfill_sources_action)
        search_google_action = QAction("Search on Google\tAlt+G", menu)
        search_google_action.triggered.connect(self._on_search_selected_on_google)
        menu.addAction(search_google_action)
        refresh_tip_action = QAction("Refresh InfoTip\tAlt+I", menu)
        refresh_tip_action.triggered.connect(self._on_refresh_selected_infotips)
        menu.addAction(refresh_tip_action)
        edit_tip_action = QAction("Manual InfoTip Entry...\tAlt+E", menu)
        edit_tip_action.triggered.connect(self._on_edit_selected_infotip)
        menu.addAction(edit_tip_action)
        assign_steamid_action = QAction("Assign SteamID (Selected)...", menu)
        assign_steamid_action.triggered.connect(self._on_assign_steam_appid_selected)
        menu.addAction(assign_steamid_action)
        assign_steamid_all_action = QAction("Assign SteamID (All Visible)...", menu)
        assign_steamid_all_action.triggered.connect(self._on_assign_steam_appid_all_visible)
        menu.addAction(assign_steamid_all_action)
        recheck_ids_action = QAction("Recheck IDs + Stores (Selected)...", menu)
        recheck_ids_action.triggered.connect(self._on_recheck_ids_and_stores_selected)
        menu.addAction(recheck_ids_action)
        recheck_ids_all_action = QAction("Recheck IDs + Stores (All Visible)...", menu)
        recheck_ids_all_action.triggered.connect(self._on_recheck_ids_and_stores_all_visible)
        menu.addAction(recheck_ids_all_action)
        store_action = QAction("Set Owned Store(s)...", menu)
        store_action.triggered.connect(self._on_set_owned_stores_selected)
        menu.addAction(store_action)
        rename_action = QAction("Edit Name...\tF2", menu)
        rename_action.triggered.connect(self._on_manual_rename_selected_entry)
        menu.addAction(rename_action)
        delete_action = QAction("Delete Selected\tCtrl+Delete", menu)
        delete_action.triggered.connect(self._on_delete_selected_entries)
        menu.addAction(delete_action)
        menu.exec(self.right_table.viewport().mapToGlobal(pos))

    def _on_right_icon_context_menu(self, pos) -> None:
        index = self.right_icon_list.indexAt(pos)
        if index.isValid():
            item = self.right_icon_list.item(index.row())
            if item is not None and not item.isSelected():
                self.right_icon_list.clearSelection()
                item.setSelected(True)

        menu = QMenu(self.right_icon_list)
        open_action = QAction("Open Folder/Archive\tCtrl+O", menu)
        open_action.triggered.connect(self._on_open_selected_in_explorer)
        menu.addAction(open_action)
        menu.addSeparator()
        assign_icon_action = QAction("Assign Folder Icon...\tCtrl+I", menu)
        assign_icon_action.triggered.connect(self._on_assign_folder_icon_selected)
        menu.addAction(assign_icon_action)
        sgdb_status_action = QAction("SteamGridDB Status...", menu)
        sgdb_status_action.triggered.connect(self._on_steamgriddb_status_selected)
        menu.addAction(sgdb_status_action)
        sgdb_upload_action = QAction("Upload Icon to SteamGridDB...", menu)
        sgdb_upload_action.triggered.connect(self._on_upload_icon_to_steamgriddb_selected)
        menu.addAction(sgdb_upload_action)
        sgdb_upload_missing_action = QAction("Upload Missing Icons to SteamGridDB...", menu)
        sgdb_upload_missing_action.triggered.connect(self._on_upload_missing_icons_to_steamgriddb_selected)
        menu.addAction(sgdb_upload_missing_action)
        rebuild_icons_action = QAction("Rebuild Existing Icons\tCtrl+Shift+B", menu)
        rebuild_icons_action.triggered.connect(self._on_rebuild_existing_icons)
        menu.addAction(rebuild_icons_action)
        backfill_sources_action = QAction("Backfill Missing Icon Sources\tCtrl+Shift+U", menu)
        backfill_sources_action.triggered.connect(self._on_backfill_missing_icon_sources)
        menu.addAction(backfill_sources_action)
        search_google_action = QAction("Search on Google\tAlt+G", menu)
        search_google_action.triggered.connect(self._on_search_selected_on_google)
        menu.addAction(search_google_action)
        refresh_tip_action = QAction("Refresh InfoTip\tAlt+I", menu)
        refresh_tip_action.triggered.connect(self._on_refresh_selected_infotips)
        menu.addAction(refresh_tip_action)
        edit_tip_action = QAction("Manual InfoTip Entry...\tAlt+E", menu)
        edit_tip_action.triggered.connect(self._on_edit_selected_infotip)
        menu.addAction(edit_tip_action)
        assign_steamid_action = QAction("Assign SteamID (Selected)...", menu)
        assign_steamid_action.triggered.connect(self._on_assign_steam_appid_selected)
        menu.addAction(assign_steamid_action)
        assign_steamid_all_action = QAction("Assign SteamID (All Visible)...", menu)
        assign_steamid_all_action.triggered.connect(self._on_assign_steam_appid_all_visible)
        menu.addAction(assign_steamid_all_action)
        recheck_ids_action = QAction("Recheck IDs + Stores (Selected)...", menu)
        recheck_ids_action.triggered.connect(self._on_recheck_ids_and_stores_selected)
        menu.addAction(recheck_ids_action)
        recheck_ids_all_action = QAction("Recheck IDs + Stores (All Visible)...", menu)
        recheck_ids_all_action.triggered.connect(self._on_recheck_ids_and_stores_all_visible)
        menu.addAction(recheck_ids_all_action)
        store_action = QAction("Set Owned Store(s)...", menu)
        store_action.triggered.connect(self._on_set_owned_stores_selected)
        menu.addAction(store_action)
        rename_action = QAction("Edit Name...\tF2", menu)
        rename_action.triggered.connect(self._on_manual_rename_selected_entry)
        menu.addAction(rename_action)
        delete_action = QAction("Delete Selected\tCtrl+Delete", menu)
        delete_action.triggered.connect(self._on_delete_selected_entries)
        menu.addAction(delete_action)
        menu.exec(self.right_icon_list.viewport().mapToGlobal(pos))

    def _selected_right_entries(self) -> list[InventoryItem]:
        rows: list[int] = []
        if self._right_view_mode == "icons":
            rows = sorted(
                {
                    self.right_icon_list.row(item)
                    for item in self.right_icon_list.selectedItems()
                }
            )
        else:
            model = self.right_table.selectionModel()
            if model is None:
                return []
            rows = sorted({idx.row() for idx in model.selectedRows()})
        return [
            self._visible_right_items[row]
            for row in rows
            if 0 <= row < len(self._visible_right_items)
        ]

    def _on_open_selected_in_explorer(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            QMessageBox.information(
                self,
                "Open Folder/Archive",
                "Select at least one game entry in the right pane.",
            )
            return
        # Open only the first selected entry to avoid unintentionally spawning
        # many Explorer windows.
        self._open_in_explorer(selected[0].full_path)

    def _on_search_selected_on_google(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            QMessageBox.information(
                self,
                "Search on Google",
                "Select at least one game entry in the right pane.",
            )
            return
        entry = selected[0]
        query = f"{(entry.cleaned_name.strip() or entry.full_name.strip() or 'game')} game"
        url = f"https://www.google.com/search?q={quote_plus(query)}"
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Search on Google",
                f"Could not open browser for query:\n{query}\n\n{exc}",
            )

    def _on_set_owned_stores_selected(self) -> None:
        selected = [entry for entry in self._selected_right_entries() if entry.is_dir]
        if not selected:
            QMessageBox.information(
                self,
                "Set Owned Store(s)",
                "Select at least one game folder first.",
            )
            return
        available = self.state.available_store_names()
        current = ", ".join(selected[0].owned_stores)
        prompt = (
            "Comma-separated stores.\n"
            f"Available: {', '.join(available)}\n"
            "Leave empty to clear ownership links."
        )
        raw, ok = QInputDialog.getText(
            self,
            "Set Owned Store(s)",
            prompt,
            text=current,
        )
        if not ok:
            return
        tokens = [
            normalize_store_name(token)
            for token in str(raw or "").replace(";", ",").split(",")
        ]
        stores = sort_stores([token for token in tokens if token])
        updated = 0
        for entry in selected:
            updated += self.state.set_manual_owned_stores(entry.full_path, stores)
        self.refresh_all()
        if len(selected) == 1 and updated > 0:
            return
        self._show_success_popup(
            "Set Owned Store(s)",
            f"Updated {len(selected)} game(s). Assigned stores per game: {len(stores)}",
        )

    @staticmethod
    def _store_from_strip_click(
        stores: list[str],
        *,
        local_x: int,
        badge_size: int = 16,
        spacing: int = 2,
    ) -> str | None:
        ordered = sort_stores(list(stores or []))
        if not ordered:
            return None
        x = int(local_x)
        if x < 0:
            return None
        step = badge_size + spacing
        for idx, store in enumerate(ordered):
            start = idx * step
            end = start + badge_size
            if start <= x < end:
                return store
        return None

    def _open_store_page_for_entry(self, entry: InventoryItem, store_name: str) -> bool:
        canonical = normalize_store_name(store_name)
        if not canonical:
            return False
        url = self.state.store_page_url_for_inventory(
            entry.full_path,
            store_name=canonical,
            game_title=entry.cleaned_name or entry.full_name,
        )
        if not url:
            return False
        try:
            webbrowser.open(url, new=2)
            return True
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Open Store Page",
                f"Could not open store page:\n{url}\n\n{exc}",
            )
            return False

    def _delete_path(self, full_path: str) -> None:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
            return
        os.remove(full_path)

    def _on_manual_rename_selected_entry(self) -> None:
        selected = self._selected_right_entries()
        if len(selected) != 1:
            QMessageBox.information(
                self,
                "Select One Entry",
                "Select exactly one row to use manual rename.",
            )
            return
        entry = selected[0]
        old_path = os.path.normpath(entry.full_path)
        parent_dir = os.path.dirname(old_path)
        old_name = os.path.basename(old_path)

        new_name, ok = QInputDialog.getText(
            self,
            "Manual Rename",
            "New name:",
            text=old_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "Invalid Name", "New name cannot be empty.")
            return
        if any(ch in new_name for ch in ("/", "\\")):
            QMessageBox.warning(
                self, "Invalid Name", "New name cannot include path separators."
            )
            return
        if new_name == old_name:
            return
        new_path = os.path.join(parent_dir, new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(
                self,
                "Name Conflict",
                f"Destination already exists:\n{new_path}",
            )
            return
        confirm = QMessageBox.question(
            self,
            "Confirm Rename",
            "Rename this item on disk?\n\n"
            f"From: {old_name}\n"
            f"To:   {new_name}\n\n"
            f"Folder: {parent_dir}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            os.rename(old_path, new_path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Rename Failed",
                f"Could not rename:\n{old_path}\n\nto:\n{new_path}\n\n{exc}",
            )
            return
        if entry.is_dir and entry.icon_status == "valid":
            renamed_cleaned = cleaned_name_from_full(
                full_name=new_name,
                is_file=False,
                approved_tags=self.state.approved_tags(),
            )
            started = self._run_infotip_refresh_operation(
                [(new_path, renamed_cleaned)],
                title="Refresh renamed InfoTip",
                show_summary=False,
            )
            if not started:
                self.refresh_all()
            return
        self.refresh_all()

    def _delete_path_with_elevation(self, full_path: str) -> tuple[bool, str]:
        path = os.path.normpath(full_path)
        if not os.path.exists(path):
            return True, ""

        def _ps_quote(value: str) -> str:
            return value.replace("'", "''")

        py_exe = _ps_quote(sys.executable)
        target = _ps_quote(path)
        script = (
            "$p = Start-Process "
            f"-FilePath '{py_exe}' "
            "-ArgumentList @('-m','gamemanager.services.elevated_delete','--path',"
            f"'{target}') "
            "-Verb RunAs -PassThru -Wait; "
            "exit $p.ExitCode"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return False, str(exc)
        if proc.returncode == 0 and not os.path.exists(path):
            return True, ""
        err = (proc.stderr or proc.stdout or "").strip()
        if not err:
            err = f"elevated delete returned code {proc.returncode}"
        return False, err

    def _on_delete_selected_entries(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            QMessageBox.information(self, "No Selection", "Select one or more rows to delete.")
            return

        root_info_by_id = {info.root_id: info for info in self.root_infos}
        selected_groups: dict[str, list[InventoryItem]] = {}
        visible_groups: dict[str, list[InventoryItem]] = {}
        for entry in self._visible_right_items:
            key = entry.cleaned_name.strip().casefold()
            visible_groups.setdefault(key, []).append(entry)
        for entry in selected:
            key = entry.cleaned_name.strip().casefold()
            selected_groups.setdefault(key, []).append(entry)

        final_delete_paths: set[str] = set()
        for key, group_items in selected_groups.items():
            visible_group = visible_groups.get(key, [])
            all_group_selected = (
                len(visible_group) > 1 and len(group_items) == len(visible_group)
            )
            if not all_group_selected:
                for entry in group_items:
                    final_delete_paths.add(entry.full_path)
                continue

            rows = [
                (entry, self._source_for_item(entry, root_info_by_id))
                for entry in visible_group
            ]
            cleaned_title = visible_group[0].cleaned_name or "(No cleaned name)"
            dialog = self._delete_group_dialog_cls(cleaned_title, rows, self)
            if dialog.exec() != dialog.DialogCode.Accepted:
                if dialog.cancel_all_requested:
                    return
                continue
            for entry in dialog.selected_for_delete():
                final_delete_paths.add(entry.full_path)

        if not final_delete_paths:
            QMessageBox.information(self, "No Deletion", "No items selected for deletion.")
            return

        answer = QMessageBox.warning(
            self,
            "Confirm Deletion",
            f"Delete {len(final_delete_paths)} selected item(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        deleted_paths: set[str] = set()
        failed: list[str] = []
        sorted_paths = sorted(final_delete_paths)
        self._begin_interactive_operation("Delete selected", len(sorted_paths))
        canceled_by_user = False
        try:
            for idx, full_path in enumerate(sorted_paths, start=1):
                if self._step_interactive_operation("Delete selected", idx - 1, len(sorted_paths)):
                    canceled_by_user = True
                    break
                try:
                    self._delete_path(full_path)
                    deleted += 1
                    deleted_paths.add(full_path)
                except OSError as exc:
                    elevated_ok, elevated_err = self._delete_path_with_elevation(full_path)
                    if elevated_ok:
                        deleted += 1
                        deleted_paths.add(full_path)
                        continue
                    details = f"{full_path}: {exc}"
                    if elevated_err:
                        details += f" | elevated retry failed: {elevated_err}"
                    failed.append(details)
        finally:
            self._end_interactive_operation()

        if deleted > 0:
            deleted_norm = {
                os.path.normcase(os.path.normpath(path))
                for path in deleted_paths
            }
            self.inventory = [
                item
                for item in self.inventory
                if os.path.normcase(os.path.normpath(item.full_path)) not in deleted_norm
            ]
            self._loaded_entries_count = len(self.inventory)
            self._populate_right(self.inventory)
            self._mark_refresh_needed(True)
        if failed:
            details = "\n".join(failed[:8])
            QMessageBox.warning(
                self,
                "Deletion Completed with Errors",
                f"Deleted: {deleted}\nFailed: {len(failed)}\n\n{details}",
            )
            return
        if canceled_by_user:
            QMessageBox.information(
                self,
                "Deletion Canceled",
                f"Deleted before cancel: {deleted}",
            )
            return
        self._show_success_popup("Deletion Completed", f"Deleted: {deleted}")
