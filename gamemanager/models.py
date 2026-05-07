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
    total_size_bytes: int
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
    icon_status: str = "none"
    folder_icon_path: str | None = None
    desktop_ini_path: str | None = None
    info_tip: str = ""
    owned_stores: list[str] = field(default_factory=list)
    primary_store: str | None = None


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


@dataclass(slots=True)
class IconCandidate:
    provider: str
    candidate_id: str
    title: str
    preview_url: str
    image_url: str
    width: int
    height: int
    has_alpha: bool
    source_url: str


@dataclass(slots=True)
class IconApplyResult:
    folder_path: str
    status: str
    message: str
    ico_path: str | None = None
    desktop_ini_path: str | None = None


@dataclass(slots=True)
class IconRebuildEntry:
    folder_path: str
    icon_path: str
    already_rebuilt: bool
    summary: str


@dataclass(slots=True)
class SgdbGameCandidate:
    game_id: int
    title: str
    confidence: float
    evidence: list[str]
    steam_appid: str | None = None
    identity_store: str | None = None
    identity_store_id: str | None = None
    store_ids: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SgdbGameBinding:
    folder_path: str
    game_id: int
    game_name: str
    last_confidence: float
    evidence_json: str
    confirmed_at: str
    updated_at: str


@dataclass(slots=True)
class SgdbIconAsset:
    icon_id: int
    url: str
    thumb_url: str
    author_name: str
    author_steam64: str


@dataclass(slots=True)
class SgdbTargetResolution:
    selected: SgdbGameCandidate | None
    candidates: list[SgdbGameCandidate]
    saved_binding: SgdbGameBinding | None
    drift_reasons: list[str]
    requires_confirmation: bool
    exact_appid_game_id: int | None = None


@dataclass(slots=True)
class SgdbOriginStatus:
    source_kind: str
    source_provider: str
    is_sgdb_origin: bool
    confidence: float
    matched_icon_id: int | None
    reason: str


@dataclass(slots=True)
class StoreAccount:
    store_name: str
    account_id: str
    display_name: str
    auth_kind: str
    enabled: bool
    created_at: str
    updated_at: str
    last_sync_utc: str = ""


@dataclass(slots=True)
class StoreOwnedGame:
    store_name: str
    account_id: str
    entitlement_id: str
    title: str
    store_game_id: str = ""
    manifest_id: str = ""
    launch_uri: str = ""
    install_path: str = ""
    is_installed: bool = False
    metadata_json: str = ""
    last_seen_utc: str = ""


@dataclass(slots=True)
class StoreLink:
    inventory_path: str
    store_name: str
    account_id: str
    entitlement_id: str
    match_method: str
    confidence: float
    verified: bool
    last_verified_utc: str
    notes: str = ""


@dataclass(slots=True)
class StoreSyncRun:
    id: int
    store_name: str
    account_id: str
    started_utc: str
    completed_utc: str
    status: str
    duration_ms: int
    imported_count: int
    error_summary: str = ""
