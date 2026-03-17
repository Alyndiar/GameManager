from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class RootFolder:
    id: int
    path: str
    enabled: bool
    added_at: str


@dataclass(slots=True)
class RootDisplayInfo:
    root_id: int
    root_path: str
    source_label: str
    drive_name: str
    free_space_bytes: int
    mountpoint: str


@dataclass(slots=True)
class InventoryItem:
    root_id: int
    root_path: str
    source_label: str
    full_name: str
    full_path: str
    is_dir: bool
    extension: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime
    cleaned_name: str
    scan_ts: datetime


@dataclass(slots=True)
class TagRule:
    canonical_tag: str
    display_tag: str
    status: str
    updated_at: str


@dataclass(slots=True)
class TagCandidate:
    canonical_tag: str
    observed_tag: str
    count: int
    example_name: str
    last_seen: str


@dataclass(slots=True)
class RenamePlanItem:
    root_id: int
    src_path: Path
    proposed_name: str
    dst_path: Path
    status: str
    conflict_type: str | None = None
    manual_required: bool = False
    error: str | None = None


@dataclass(slots=True)
class MovePlanItem:
    root_id: int
    src_path: Path
    dst_folder: Path
    dst_path: Path
    status: str
    conflict_type: str | None = None
    selected_action: str = "skip"
    manual_name: str | None = None
    error: str | None = None


@dataclass(slots=True)
class OperationReport:
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)

