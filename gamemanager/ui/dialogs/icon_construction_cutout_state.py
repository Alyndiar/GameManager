from __future__ import annotations

from collections.abc import Iterable

from gamemanager.services.background_removal import normalize_background_removal_params


CUTOUT_SCOPE_VALUES = {"global", "contig"}
CUTOUT_FALLOFF_VALUES = {"flat", "lin", "smooth", "cos", "exp", "log", "gauss"}


def normalize_cutout_scope(scope: object) -> str:
    value = str(scope or "global").strip().casefold()
    return value if value in CUTOUT_SCOPE_VALUES else "global"


def normalize_cutout_falloff(falloff: object) -> str:
    value = str(falloff or "flat").strip().casefold()
    return value if value in CUTOUT_FALLOFF_VALUES else "flat"


def default_curve_strength_for_mode(mode: object) -> int:
    token = str(mode or "").strip().casefold()
    if token == "exp":
        return 35
    if token == "log":
        return 65
    if token == "gauss":
        return 45
    return 50


def cutout_mode_uses_curve_strength(mode: object) -> bool:
    return str(mode or "").strip().casefold() in {"exp", "log", "gauss"}


def any_curve_mode_entries(entries: Iterable[dict[str, object]]) -> bool:
    for entry in entries:
        if cutout_mode_uses_curve_strength(entry.get("falloff", "flat")):
            return True
    return False


def normalize_seed_points(raw_points: object) -> list[list[float]]:
    points: list[list[float]] = []
    if not isinstance(raw_points, list):
        return points
    for seed in raw_points:
        if not isinstance(seed, (list, tuple)) or len(seed) < 2:
            continue
        try:
            sx = max(0.0, min(1.0, float(seed[0])))
            sy = max(0.0, min(1.0, float(seed[1])))
        except (TypeError, ValueError):
            continue
        packed = [sx, sy]
        if packed not in points:
            points.append(packed)
    return points


def load_cutout_picked_colors_state(
    params: dict[str, object] | None,
    start_row_uid: int,
) -> tuple[list[dict[str, object]], dict[int, dict[str, list[object]]], int]:
    normalized = normalize_background_removal_params(params or {})
    entries = normalized.get("picked_colors", [])
    items: list[dict[str, object]] = []
    history: dict[int, dict[str, list[object]]] = {}
    next_row_uid = int(start_row_uid)
    if not isinstance(entries, list):
        return items, history, next_row_uid
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        color = entry.get("color")
        if not isinstance(color, (list, tuple)) or len(color) < 3:
            continue
        try:
            red = max(0, min(255, int(color[0])))
            green = max(0, min(255, int(color[1])))
            blue = max(0, min(255, int(color[2])))
            tolerance = max(0, min(30, int(entry.get("tolerance", 10) or 10)))
        except (TypeError, ValueError):
            continue

        row_id = next_row_uid
        next_row_uid += 1
        item = {
            "id": row_id,
            "color": [red, green, blue],
            "tolerance": tolerance,
            "scope": normalize_cutout_scope(entry.get("scope", "global")),
            "falloff": normalize_cutout_falloff(entry.get("falloff", "flat")),
            "include_seeds": normalize_seed_points(entry.get("include_seeds")),
            "exclude_seeds": normalize_seed_points(entry.get("exclude_seeds")),
        }
        if item not in items:
            items.append(item)
        history[row_id] = {"undo": [], "redo": []}
    return items, history, next_row_uid


def load_cutout_falloff_settings(params: dict[str, object] | None) -> tuple[bool, int]:
    normalized = normalize_background_removal_params(params or {})
    advanced = bool(normalized.get("pick_colors_advanced", False))
    strength = int(normalized.get("pick_colors_curve_strength", 50) or 50)
    return advanced, max(0, min(100, strength))


def serialize_cutout_picked_rows(
    entries: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for entry in entries:
        color = entry.get("color", [0, 0, 0])
        include_seeds = (
            list(entry.get("include_seeds", []))
            if isinstance(entry.get("include_seeds"), list)
            else []
        )
        exclude_seeds = (
            list(entry.get("exclude_seeds", []))
            if isinstance(entry.get("exclude_seeds"), list)
            else []
        )
        rows.append(
            {
                "color": list(color) if isinstance(color, (list, tuple)) else [0, 0, 0],
                "tolerance": int(entry.get("tolerance", 10) or 10),
                "scope": normalize_cutout_scope(entry.get("scope", "global")),
                "falloff": normalize_cutout_falloff(entry.get("falloff", "flat")),
                "include_seeds": include_seeds,
                "exclude_seeds": exclude_seeds,
            }
        )
    return rows


def upsert_cutout_mark_point(
    points: list[list[float]],
    point: tuple[float, float],
    *,
    epsilon: float = 0.002,
    max_points: int = 512,
) -> None:
    x_new, y_new = point
    for idx, existing in enumerate(points):
        if not isinstance(existing, (list, tuple)) or len(existing) < 2:
            continue
        if abs(float(existing[0]) - x_new) <= epsilon and abs(float(existing[1]) - y_new) <= epsilon:
            points[idx] = [x_new, y_new]
            return
    points.append([x_new, y_new])
    if len(points) > max_points:
        del points[0]
