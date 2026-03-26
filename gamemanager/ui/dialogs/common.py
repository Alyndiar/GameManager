from __future__ import annotations

from dataclasses import dataclass

from gamemanager.models import IconCandidate


@dataclass(slots=True)
class TagReviewResult:
    decisions: dict[str, str]
    display_map: dict[str, str]


@dataclass(slots=True)
class IconPickerResult:
    candidate: IconCandidate | None
    local_image_path: str | None
    source_image_bytes: bytes | None
    prepared_image_bytes: bytes | None
    prepared_is_final_composite: bool
    info_tip: str
    icon_style: str
    bg_removal_engine: str
    bg_removal_params: dict[str, object]
    text_preserve_config: dict[str, object]
    border_shader: dict[str, object]


@dataclass(slots=True)
class IconProviderSettingsResult:
    steamgriddb_enabled: bool
    steamgriddb_api_key: str
    steamgriddb_api_base: str
    iconfinder_enabled: bool
    iconfinder_api_key: str
    iconfinder_api_base: str


@dataclass(slots=True)
class PerformanceSettingsResult:
    scan_size_workers: int
    progress_interval_ms: int
    dir_cache_enabled: bool
    dir_cache_max_entries: int
    startup_prewarm_mode: str


__all__ = ["IconPickerResult", "IconProviderSettingsResult", "PerformanceSettingsResult", "TagReviewResult"]
