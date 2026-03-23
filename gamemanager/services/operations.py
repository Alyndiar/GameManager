from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from gamemanager.models import MovePlanItem, OperationReport, RenamePlanItem, RootFolder
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.normalization import normalize_separators


def _same_path(a: Path, b: Path) -> bool:
    return str(a.resolve()).casefold() == str(b.resolve()).casefold()


def _safe_cleaned_stem(value: str) -> str:
    cleaned = normalize_separators(value)
    return cleaned or value


def build_rename_plan(roots: list[RootFolder]) -> list[RenamePlanItem]:
    plan: list[RenamePlanItem] = []
    for root in roots:
        root_path = Path(root.path)
        if not root_path.exists() or not root_path.is_dir():
            continue
        for child in root_path.iterdir():
            is_file = child.is_file()
            if is_file:
                proposed = f"{_safe_cleaned_stem(child.stem)}{child.suffix}"
            else:
                proposed = _safe_cleaned_stem(child.name)
            destination = child.with_name(proposed)
            if proposed == child.name:
                status = "unchanged"
                conflict_type = None
                manual = False
            elif destination.exists() and not _same_path(destination, child):
                status = "conflict"
                conflict_type = "destination_exists"
                manual = True
            else:
                status = "ready"
                conflict_type = None
                manual = False
            plan.append(
                RenamePlanItem(
                    root_id=root.id,
                    src_path=child,
                    proposed_name=proposed,
                    dst_path=destination,
                    status=status,
                    conflict_type=conflict_type,
                    manual_required=manual,
                )
            )
    return plan


def execute_rename_plan(
    plan_items: list[RenamePlanItem],
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> OperationReport:
    def _emit(current: int, total: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb("Cleanup names", current, total)
        except Exception:
            return

    report = OperationReport(total=len(plan_items))
    total = len(plan_items)
    _emit(0, total)
    if should_cancel is not None and should_cancel():
        raise OperationCancelled("Cleanup canceled")
    for idx, item in enumerate(plan_items, start=1):
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("Cleanup canceled")
        if item.status == "unchanged":
            report.skipped += 1
        elif item.status == "conflict":
            report.conflicts += 1
            report.details.append(f"Conflict: {item.src_path} -> {item.dst_path}")
        elif item.status != "ready":
            report.skipped += 1
        else:
            try:
                item.src_path.rename(item.dst_path)
                report.succeeded += 1
            except FileExistsError:
                report.conflicts += 1
                report.details.append(f"Conflict: {item.src_path} -> {item.dst_path}")
            except OSError as exc:
                report.failed += 1
                report.details.append(f"Failed rename {item.src_path}: {exc}")
        _emit(idx, total)
    return report


def build_move_plan(
    roots: list[RootFolder], allowed_extensions: set[str]
) -> list[MovePlanItem]:
    normalized_ext = {ext if ext.startswith(".") else f".{ext}" for ext in allowed_extensions}
    normalized_ext = {ext.casefold() for ext in normalized_ext}
    plan: list[MovePlanItem] = []
    for root in roots:
        root_path = Path(root.path)
        if not root_path.exists() or not root_path.is_dir():
            continue
        for child in root_path.iterdir():
            if not child.is_file():
                continue
            if child.suffix.casefold() not in normalized_ext:
                continue
            folder_name = _safe_cleaned_stem(child.stem)
            dst_folder = root_path / folder_name
            dst_path = dst_folder / child.name
            status = "ready"
            conflict_type = None
            if dst_folder.exists() and not dst_folder.is_dir():
                status = "conflict"
                conflict_type = "destination_folder_is_file"
            elif dst_path.exists():
                status = "conflict"
                conflict_type = "destination_file_exists"
            selected_action = "move" if status == "ready" else "skip"
            plan.append(
                MovePlanItem(
                    root_id=root.id,
                    src_path=child,
                    dst_folder=dst_folder,
                    dst_path=dst_path,
                    status=status,
                    conflict_type=conflict_type,
                    selected_action=selected_action,
                )
            )
    return plan


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def execute_move_plan(
    plan_items: list[MovePlanItem],
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> OperationReport:
    def _emit(current: int, total: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb("Move archives", current, total)
        except Exception:
            return

    report = OperationReport(total=len(plan_items))
    total = len(plan_items)
    _emit(0, total)
    if should_cancel is not None and should_cancel():
        raise OperationCancelled("Move canceled")
    for idx, item in enumerate(plan_items, start=1):
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("Move canceled")
        action = item.selected_action
        if item.status == "conflict" and action == "skip":
            report.conflicts += 1
            report.details.append(f"Conflict skipped: {item.src_path} -> {item.dst_path}")
        elif action == "skip":
            report.skipped += 1
        else:
            try:
                item.dst_folder.mkdir(parents=True, exist_ok=True)
                destination = item.dst_path
                if action == "rename":
                    if not item.manual_name:
                        raise OSError("Manual rename selected without manual name")
                    destination = item.dst_folder / item.manual_name
                if action in {"overwrite", "delete_destination"}:
                    _remove_path(destination)
                if destination.exists():
                    raise FileExistsError(f"Destination exists: {destination}")
                shutil.move(str(item.src_path), str(destination))
                report.succeeded += 1
            except FileExistsError as exc:
                report.conflicts += 1
                report.details.append(f"Conflict: {exc}")
            except OSError as exc:
                report.failed += 1
                report.details.append(f"Failed move {item.src_path}: {exc}")
        _emit(idx, total)
    return report
