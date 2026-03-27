from __future__ import annotations

import colorsys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import filecmp
from functools import lru_cache
import gc
from io import BytesIO, StringIO
import math
import os
from pathlib import Path
import shutil
import threading
import warnings

from PIL import Image, ImageFilter, ImageOps

from gamemanager.services.paths import project_data_dir


BACKGROUND_REMOVAL_OPTIONS: list[tuple[str, str]] = [
    ("Disabled", "none"),
    ("Remove Colors", "pick_colors"),
    ("rembg (U2Net)", "rembg"),
    ("BRIA RMBG-2.0", "bria_rmbg"),
]

DEFAULT_BG_REMOVAL_PARAMS: dict[str, object] = {
    "alpha_matting": False,
    "alpha_matting_foreground_threshold": 220,
    "alpha_matting_background_threshold": 8,
    "alpha_matting_erode_size": 1,
    "alpha_edge_feather": 0,
    "post_process_mask": False,
    "picked_colors": [],
    "pick_colors_use_hsv": True,
    "pick_colors_tolerance_mode": "max",
    "pick_colors_edge_flood_fill": False,
    "pick_colors_falloff": "flat",
    "pick_colors_curve_strength": 50,
    "pick_colors_advanced": False,
}


_PICK_COLOR_FALLOFF_ALIASES: dict[str, str] = {
    "linear": "lin",
    "cosine": "cos",
    "gaussian": "gauss",
}
_PICK_COLOR_FALLOFF_VALUES: set[str] = {"flat", "lin", "smooth", "cos", "exp", "log", "gauss"}
_PICK_COLOR_SCOPE_VALUES: set[str] = {"global", "contig"}


def _remove_if_empty(path: Path) -> None:
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        return


def _merge_move_dir(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        src_path = src / entry.name
        dst_path = dst / entry.name
        if src_path.is_dir():
            _merge_move_dir(src_path, dst_path)
            _remove_if_empty(src_path)
            continue
        if dst_path.exists():
            try:
                if filecmp.cmp(src_path, dst_path, shallow=False):
                    src_path.unlink()
            except OSError:
                pass
            continue
        try:
            shutil.move(str(src_path), str(dst_path))
        except OSError:
            continue
    _remove_if_empty(src)


def _configure_local_model_cache() -> None:
    model_root = project_data_dir() / "models"
    model_root.mkdir(parents=True, exist_ok=True)

    u2net_home = model_root / "u2net"
    hf_home = model_root / "hf"
    torch_home = model_root / "torch"
    xdg_home = model_root / "xdg"
    transformers_home = hf_home / "transformers"
    u2net_home.mkdir(parents=True, exist_ok=True)
    hf_home.mkdir(parents=True, exist_ok=True)
    torch_home.mkdir(parents=True, exist_ok=True)
    xdg_home.mkdir(parents=True, exist_ok=True)
    transformers_home.mkdir(parents=True, exist_ok=True)

    # Migrate common legacy caches from user-profile locations.
    home = Path.home()
    _merge_move_dir(home / ".u2net", u2net_home)
    _merge_move_dir(home / ".cache" / "huggingface", hf_home)
    _merge_move_dir(home / ".cache" / "torch", torch_home)

    os.environ.setdefault("U2NET_HOME", str(u2net_home))
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("HF_HUB_CACHE", str(hf_home / "hub"))
    os.environ.setdefault("TORCH_HOME", str(torch_home))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(transformers_home))
    os.environ.setdefault("XDG_CACHE_HOME", str(xdg_home))


_configure_local_model_cache()

_BACKGROUND_MODEL_LOCK = threading.Lock()
_ACTIVE_BACKGROUND_MODELS: set[str] = set()
_PARKED_BACKGROUND_MODELS: dict[str, object] = {}


def normalize_background_removal_engine(engine: str | None) -> str:
    value = (engine or "").strip().casefold()
    if value in {"none", "pick_colors", "rembg", "bria_rmbg"}:
        return value
    return "none"


def background_removal_device_status(engine: str | None) -> str:
    mode = normalize_background_removal_engine(engine)
    if mode == "none":
        return "Disabled"
    preferred = _preferred_onnx_providers()
    if "CUDAExecutionProvider" in preferred:
        return "GPU (CUDA)"
    if preferred:
        return "CPU"
    return "Unavailable"


def normalize_background_removal_params(
    params: dict[str, object] | None,
) -> dict[str, object]:
    raw = dict(DEFAULT_BG_REMOVAL_PARAMS)
    if isinstance(params, dict):
        raw.update(params)
    pick_mode = str(raw.get("pick_colors_tolerance_mode", "max") or "max").strip().casefold()
    if pick_mode not in {"max", "euclidean"}:
        pick_mode = "max"
    pick_falloff = str(raw.get("pick_colors_falloff", "flat") or "flat").strip().casefold()
    pick_falloff = _PICK_COLOR_FALLOFF_ALIASES.get(pick_falloff, pick_falloff)
    if pick_falloff not in _PICK_COLOR_FALLOFF_VALUES:
        pick_falloff = "flat"
    pick_curve_strength = max(0, min(100, int(raw.get("pick_colors_curve_strength", 50) or 50)))
    pick_entries_raw = raw.get("picked_colors")
    pick_entries: list[dict[str, object]] = []
    if isinstance(pick_entries_raw, list):
        for item in pick_entries_raw:
            if not isinstance(item, dict):
                continue
            color_raw = item.get("color")
            if not isinstance(color_raw, (list, tuple)) or len(color_raw) < 3:
                continue
            try:
                red = max(0, min(255, int(color_raw[0])))
                green = max(0, min(255, int(color_raw[1])))
                blue = max(0, min(255, int(color_raw[2])))
                tolerance = max(0, min(30, int(item.get("tolerance", 10) or 10)))
            except (TypeError, ValueError):
                continue
            entry_falloff = str(item.get("falloff", pick_falloff) or pick_falloff).strip().casefold()
            entry_falloff = _PICK_COLOR_FALLOFF_ALIASES.get(entry_falloff, entry_falloff)
            if entry_falloff not in _PICK_COLOR_FALLOFF_VALUES:
                entry_falloff = pick_falloff
            entry_scope = str(item.get("scope", "global") or "global").strip().casefold()
            if entry_scope not in _PICK_COLOR_SCOPE_VALUES:
                entry_scope = "global"
            include_raw = item.get("include_seeds")
            exclude_raw = item.get("exclude_seeds")
            include_seeds: list[list[float]] = []
            exclude_seeds: list[list[float]] = []
            if isinstance(include_raw, list):
                for seed in include_raw:
                    if not isinstance(seed, (list, tuple)) or len(seed) < 2:
                        continue
                    try:
                        sx = max(0.0, min(1.0, float(seed[0])))
                        sy = max(0.0, min(1.0, float(seed[1])))
                    except (TypeError, ValueError):
                        continue
                    packed = [sx, sy]
                    if packed not in include_seeds:
                        include_seeds.append(packed)
            if isinstance(exclude_raw, list):
                for seed in exclude_raw:
                    if not isinstance(seed, (list, tuple)) or len(seed) < 2:
                        continue
                    try:
                        sx = max(0.0, min(1.0, float(seed[0])))
                        sy = max(0.0, min(1.0, float(seed[1])))
                    except (TypeError, ValueError):
                        continue
                    packed = [sx, sy]
                    if packed not in exclude_seeds:
                        exclude_seeds.append(packed)
            normalized = {
                "color": [red, green, blue],
                "tolerance": tolerance,
                "scope": entry_scope,
                "falloff": entry_falloff,
                "include_seeds": include_seeds,
                "exclude_seeds": exclude_seeds,
            }
            if normalized not in pick_entries:
                pick_entries.append(normalized)
    return {
        "alpha_matting": bool(raw.get("alpha_matting", False)),
        "alpha_matting_foreground_threshold": max(
            1, min(255, int(raw.get("alpha_matting_foreground_threshold", 220) or 220))
        ),
        "alpha_matting_background_threshold": max(
            0, min(254, int(raw.get("alpha_matting_background_threshold", 8) or 8))
        ),
        "alpha_matting_erode_size": max(
            0, min(64, int(raw.get("alpha_matting_erode_size", 1) or 1))
        ),
        "alpha_edge_feather": max(0, min(24, int(raw.get("alpha_edge_feather", 0) or 0))),
        "post_process_mask": bool(raw.get("post_process_mask", False)),
        "picked_colors": pick_entries,
        "pick_colors_use_hsv": bool(raw.get("pick_colors_use_hsv", True)),
        "pick_colors_tolerance_mode": pick_mode,
        "pick_colors_edge_flood_fill": bool(raw.get("pick_colors_edge_flood_fill", False)),
        "pick_colors_falloff": pick_falloff,
        "pick_colors_curve_strength": pick_curve_strength,
        "pick_colors_advanced": bool(raw.get("pick_colors_advanced", False)),
    }


def _pick_color_threshold(color: tuple[int, int, int], tolerance: int) -> int:
    level = max(0, min(30, int(tolerance)))
    if color in {(0, 0, 0), (255, 255, 255)}:
        return level
    return max(0, min(255, int(round(level / 2.0))))


def _rgb_to_hsv255(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    red = max(0, min(255, int(rgb[0]))) / 255.0
    green = max(0, min(255, int(rgb[1]))) / 255.0
    blue = max(0, min(255, int(rgb[2]))) / 255.0
    hue, sat, val = colorsys.rgb_to_hsv(red, green, blue)
    return (
        int(round(hue * 255.0)) % 256,
        int(round(sat * 255.0)),
        int(round(val * 255.0)),
    )


def _hue_diff(a: int, b: int) -> int:
    delta = abs(int(a) - int(b)) % 256
    return min(delta, 256 - delta)


def _color_distance(
    rgb: tuple[int, int, int],
    ref_rgb: tuple[int, int, int],
    *,
    mode: str,
    space: str,
    hsv_rgb: tuple[int, int, int] | None = None,
    hsv_ref: tuple[int, int, int] | None = None,
) -> float:
    if space == "hsv":
        hsv_val = hsv_rgb if hsv_rgb is not None else _rgb_to_hsv255(rgb)
        hsv_target = hsv_ref if hsv_ref is not None else _rgb_to_hsv255(ref_rgb)
        dh = _hue_diff(hsv_val[0], hsv_target[0])
        ds = abs(hsv_val[1] - hsv_target[1])
        dv = abs(hsv_val[2] - hsv_target[2])
        if mode == "euclidean":
            return float((dh * dh + ds * ds + dv * dv) ** 0.5)
        return float(max(dh, ds, dv))
    dr = int(rgb[0]) - int(ref_rgb[0])
    dg = int(rgb[1]) - int(ref_rgb[1])
    db = int(rgb[2]) - int(ref_rgb[2])
    if mode == "euclidean":
        return float((dr * dr + dg * dg + db * db) ** 0.5)
    return float(max(abs(dr), abs(dg), abs(db)))


def _curve_param_from_strength(strength: int) -> float:
    s = max(0, min(100, int(strength)))
    k_min = 0.35
    k_max = 8.0
    ratio = (k_max / k_min) ** (s / 100.0)
    return k_min * ratio


def _sigma_from_strength(strength: int) -> float:
    s = max(0, min(100, int(strength)))
    sigma_max = 1.2
    sigma_min = 0.15
    ratio = (sigma_min / sigma_max) ** (s / 100.0)
    return sigma_max * ratio


def _falloff_removal(
    distance: float,
    *,
    tolerance: int,
    mode: str,
    curve_strength: int,
) -> float:
    tol = max(0, int(tolerance))
    d = max(0.0, float(distance))
    normalized_mode = str(mode or "flat").strip().casefold()
    normalized_mode = _PICK_COLOR_FALLOFF_ALIASES.get(normalized_mode, normalized_mode)
    if normalized_mode == "flat":
        return 1.0 if d <= float(tol) else 0.0

    radius = float(tol + 1)
    if radius <= 0.0:
        return 0.0
    u = max(0.0, min(1.0, d / radius))
    if normalized_mode == "lin":
        return max(0.0, 1.0 - u)
    if normalized_mode == "smooth":
        smooth = (3.0 * u * u) - (2.0 * u * u * u)
        return max(0.0, min(1.0, 1.0 - smooth))
    if normalized_mode == "cos":
        return max(0.0, min(1.0, math.cos((math.pi * 0.5) * u)))
    if normalized_mode == "exp":
        k = _curve_param_from_strength(curve_strength)
        den = 1.0 - math.exp(-k)
        if den <= 1e-9:
            return max(0.0, 1.0 - u)
        num = math.exp(-k * u) - math.exp(-k)
        return max(0.0, min(1.0, num / den))
    if normalized_mode == "log":
        k = _curve_param_from_strength(curve_strength)
        den = math.log1p(k)
        if den <= 1e-9:
            return max(0.0, 1.0 - u)
        return max(0.0, min(1.0, 1.0 - (math.log1p(k * u) / den)))
    if normalized_mode == "gauss":
        sigma = _sigma_from_strength(curve_strength)
        if sigma <= 1e-9:
            return max(0.0, 1.0 - u)
        g_u = math.exp(-(u * u) / (2.0 * sigma * sigma))
        g_1 = math.exp(-1.0 / (2.0 * sigma * sigma))
        den = 1.0 - g_1
        if den <= 1e-9:
            return max(0.0, 1.0 - u)
        return max(0.0, min(1.0, (g_u - g_1) / den))
    return max(0.0, 1.0 - u)


def _seed_to_index(
    seed_xy: list[float] | tuple[float, float] | None,
    *,
    width: int,
    height: int,
) -> int | None:
    if seed_xy is None:
        return None
    try:
        sx = max(0.0, min(1.0, float(seed_xy[0])))  # type: ignore[index]
        sy = max(0.0, min(1.0, float(seed_xy[1])))  # type: ignore[index]
    except Exception:
        return None
    px = int(round(sx * max(0, width - 1)))
    py = int(round(sy * max(0, height - 1)))
    if px < 0 or py < 0 or px >= width or py >= height:
        return None
    return (py * width) + px


def _resolve_seed_candidate_index(
    candidate: list[bool],
    *,
    width: int,
    height: int,
    seed_idx: int,
    max_radius: int = 3,
) -> int | None:
    if seed_idx < 0 or seed_idx >= len(candidate):
        return None
    if candidate[seed_idx]:
        return seed_idx
    x0 = seed_idx % width
    y0 = seed_idx // width
    for radius in range(1, max(1, max_radius) + 1):
        x_min = max(0, x0 - radius)
        x_max = min(width - 1, x0 + radius)
        y_min = max(0, y0 - radius)
        y_max = min(height - 1, y0 + radius)
        for y in range(y_min, y_max + 1):
            for x in range(x_min, x_max + 1):
                idx = (y * width) + x
                if candidate[idx]:
                    return idx
    return None


def _component_mask_from_seed(
    candidate: list[bool],
    *,
    width: int,
    height: int,
    seed_idx: int,
) -> list[bool]:
    total = width * height
    out = [False] * total
    if seed_idx < 0 or seed_idx >= total or not candidate[seed_idx]:
        return out
    queue: list[int] = [seed_idx]
    out[seed_idx] = True
    head = 0
    while head < len(queue):
        idx = queue[head]
        head += 1
        x = idx % width
        y = idx // width
        if x > 0:
            left = idx - 1
            if candidate[left] and not out[left]:
                out[left] = True
                queue.append(left)
        if x + 1 < width:
            right = idx + 1
            if candidate[right] and not out[right]:
                out[right] = True
                queue.append(right)
        if y > 0:
            up = idx - width
            if candidate[up] and not out[up]:
                out[up] = True
                queue.append(up)
        if y + 1 < height:
            down = idx + width
            if candidate[down] and not out[down]:
                out[down] = True
                queue.append(down)
    return out


def _edge_connected_mask(candidate: list[bool], *, width: int, height: int) -> list[bool]:
    total = width * height
    out = [False] * total
    queue: list[int] = []

    def _seed_idx(idx: int) -> None:
        if 0 <= idx < total and candidate[idx] and not out[idx]:
            out[idx] = True
            queue.append(idx)

    for x in range(width):
        _seed_idx(x)
        _seed_idx((height - 1) * width + x)
    for y in range(height):
        _seed_idx(y * width)
        _seed_idx(y * width + (width - 1))

    head = 0
    while head < len(queue):
        idx = queue[head]
        head += 1
        x = idx % width
        y = idx // width
        if x > 0:
            left = idx - 1
            if candidate[left] and not out[left]:
                out[left] = True
                queue.append(left)
        if x + 1 < width:
            right = idx + 1
            if candidate[right] and not out[right]:
                out[right] = True
                queue.append(right)
        if y > 0:
            up = idx - width
            if candidate[up] and not out[up]:
                out[up] = True
                queue.append(up)
        if y + 1 < height:
            down = idx + width
            if candidate[down] and not out[down]:
                out[down] = True
                queue.append(down)
    return out


def _remove_background_pick_colors(
    image_bytes: bytes,
    *,
    params: dict[str, object] | None,
) -> bytes:
    cfg = normalize_background_removal_params(params)
    entries = cfg.get("picked_colors")
    if not isinstance(entries, list) or not entries:
        return image_bytes
    use_hsv = bool(cfg.get("pick_colors_use_hsv", True))
    tolerance_mode = str(cfg.get("pick_colors_tolerance_mode", "max") or "max")
    curve_strength = int(cfg.get("pick_colors_curve_strength", 50) or 50)
    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGBA")
    except Exception:
        return image_bytes
    specs: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        color_raw = entry.get("color")
        if not isinstance(color_raw, (list, tuple)) or len(color_raw) < 3:
            continue
        try:
            color = (
                max(0, min(255, int(color_raw[0]))),
                max(0, min(255, int(color_raw[1]))),
                max(0, min(255, int(color_raw[2]))),
            )
            tolerance = int(entry.get("tolerance", 10) or 10)
        except (TypeError, ValueError):
            continue
        threshold = _pick_color_threshold(color, tolerance)
        compare_space = "hsv" if (use_hsv and color not in {(0, 0, 0), (255, 255, 255)}) else "rgb"
        falloff_mode = str(entry.get("falloff", cfg.get("pick_colors_falloff", "flat")) or "flat")
        scope = str(entry.get("scope", "global") or "global").strip().casefold()
        if scope not in _PICK_COLOR_SCOPE_VALUES:
            scope = "global"
        include_seeds = entry.get("include_seeds")
        exclude_seeds = entry.get("exclude_seeds")
        specs.append(
            {
                "color_rgb": color,
                "color_hsv": _rgb_to_hsv255(color) if compare_space == "hsv" else None,
                "threshold": int(max(0, threshold)),
                "distance_mode": "euclidean" if tolerance_mode == "euclidean" else "max",
                "compare_space": compare_space,
                "falloff": falloff_mode,
                "scope": scope,
                "include_seeds": list(include_seeds) if isinstance(include_seeds, list) else [],
                "exclude_seeds": list(exclude_seeds) if isinstance(exclude_seeds, list) else [],
            }
        )
    if not specs:
        return image_bytes

    width, height = image.size
    total = width * height
    rgba_data = bytearray(image.tobytes())
    rgb_pixels: list[tuple[int, int, int]] = []
    alpha_vals: list[int] = []
    for offset in range(0, len(rgba_data), 4):
        rgb_pixels.append(
            (
                int(rgba_data[offset]),
                int(rgba_data[offset + 1]),
                int(rgba_data[offset + 2]),
            )
        )
        alpha_vals.append(int(rgba_data[offset + 3]))
    hsv_cache: list[tuple[int, int, int] | None] = [None] * total
    max_removals: list[float] = [0.0] * total

    for spec in specs:
        threshold = int(spec["threshold"])
        mode = str(spec["distance_mode"])
        space = str(spec["compare_space"])
        falloff = str(spec["falloff"])
        color_rgb = spec["color_rgb"]  # type: ignore[assignment]
        color_hsv = spec["color_hsv"]  # type: ignore[assignment]
        removals: list[float] = [0.0] * total
        candidate: list[bool] = [False] * total
        for idx in range(total):
            if alpha_vals[idx] <= 0:
                continue
            rgb = rgb_pixels[idx]
            hsv_val: tuple[int, int, int] | None = None
            if space == "hsv":
                hsv_val = hsv_cache[idx]
                if hsv_val is None:
                    hsv_val = _rgb_to_hsv255(rgb)
                    hsv_cache[idx] = hsv_val
            distance = _color_distance(
                rgb,
                color_rgb,
                mode=mode,
                space=space,
                hsv_rgb=hsv_val,
                hsv_ref=color_hsv,
            )
            removal = _falloff_removal(
                distance,
                tolerance=threshold,
                mode=falloff,
                curve_strength=curve_strength,
            )
            if removal > 0.0:
                removals[idx] = removal
                candidate[idx] = True

        scope = str(spec["scope"])
        selected: list[bool]
        if scope == "contig":
            selected = _edge_connected_mask(candidate, width=width, height=height)
            include_seeds = spec.get("include_seeds", [])
            if isinstance(include_seeds, list):
                for seed in include_seeds:
                    seed_idx = _seed_to_index(seed, width=width, height=height)  # type: ignore[arg-type]
                    if seed_idx is None:
                        continue
                    resolved_idx = _resolve_seed_candidate_index(
                        candidate,
                        width=width,
                        height=height,
                        seed_idx=seed_idx,
                    )
                    if resolved_idx is None:
                        continue
                    comp = _component_mask_from_seed(
                        candidate,
                        width=width,
                        height=height,
                        seed_idx=resolved_idx,
                    )
                    for idx in range(total):
                        if comp[idx]:
                            selected[idx] = True
            exclude_seeds = spec.get("exclude_seeds", [])
            if isinstance(exclude_seeds, list):
                for seed in exclude_seeds:
                    seed_idx = _seed_to_index(seed, width=width, height=height)  # type: ignore[arg-type]
                    if seed_idx is None:
                        continue
                    resolved_idx = _resolve_seed_candidate_index(
                        candidate,
                        width=width,
                        height=height,
                        seed_idx=seed_idx,
                    )
                    if resolved_idx is None:
                        continue
                    comp = _component_mask_from_seed(
                        candidate,
                        width=width,
                        height=height,
                        seed_idx=resolved_idx,
                    )
                    for idx in range(total):
                        if comp[idx]:
                            selected[idx] = False
        else:
            selected = candidate

        for idx in range(total):
            if not selected[idx]:
                continue
            removal = removals[idx]
            if removal > max_removals[idx]:
                max_removals[idx] = removal

    for idx in range(total):
        removal = max_removals[idx]
        if removal <= 0.0:
            continue
        old_alpha = alpha_vals[idx]
        new_alpha = int(round(old_alpha * (1.0 - removal)))
        rgba_data[(idx * 4) + 3] = max(0, min(255, new_alpha))

    out_image = Image.frombytes("RGBA", image.size, bytes(rgba_data))
    out = BytesIO()
    out_image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _rembg_remove_kwargs(params: dict[str, object] | None) -> dict[str, object]:
    cfg = normalize_background_removal_params(params)
    return {
        "alpha_matting": bool(cfg.get("alpha_matting", False)),
        "alpha_matting_foreground_threshold": int(
            cfg.get("alpha_matting_foreground_threshold", 220)
        ),
        "alpha_matting_background_threshold": int(
            cfg.get("alpha_matting_background_threshold", 8)
        ),
        "alpha_matting_erode_size": int(cfg.get("alpha_matting_erode_size", 1)),
        "alpha_edge_feather": int(cfg.get("alpha_edge_feather", 0)),
        "post_process_mask": bool(cfg.get("post_process_mask", False)),
    }


def _tune_cutout_alpha(
    cutout_bytes: bytes,
    params: dict[str, object] | None,
) -> bytes:
    cfg = normalize_background_removal_params(params)
    try:
        with Image.open(BytesIO(cutout_bytes)) as loaded:
            loaded.load()
            image = ImageOps.exif_transpose(loaded).convert("RGBA")
    except Exception:
        return cutout_bytes

    alpha = image.getchannel("A")
    fg = int(cfg.get("alpha_matting_foreground_threshold", 220) or 220)
    bg = int(cfg.get("alpha_matting_background_threshold", 8) or 8)
    if fg <= bg:
        fg = min(255, bg + 1)

    alpha = alpha.point(
        lambda value: (
            0
            if int(value) <= bg
            else (
                255
                if int(value) >= fg
                else int(round(((int(value) - bg) * 255.0) / max(1.0, float(fg - bg))))
            )
        ),
        mode="L",
    )

    erode = int(cfg.get("alpha_matting_erode_size", 0) or 0)
    post = bool(cfg.get("post_process_mask", False))
    if erode > 0 or post:
        try:
            import cv2
            import numpy as np

            arr = np.array(alpha, dtype=np.uint8)
            if erode > 0:
                k = max(1, (min(64, erode) * 2) + 1)
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                arr = cv2.erode(arr, kernel, iterations=1)
            if post:
                k2 = max(3, (max(1, min(12, erode)) * 2) + 1)
                kernel2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2))
                arr = cv2.morphologyEx(arr, cv2.MORPH_OPEN, kernel2, iterations=1)
                arr = cv2.morphologyEx(arr, cv2.MORPH_CLOSE, kernel2, iterations=1)
            alpha = Image.fromarray(arr, mode="L")
        except Exception:
            if erode > 0:
                min_size = max(3, (min(16, erode) * 2) + 1)
                alpha = alpha.filter(ImageFilter.MinFilter(size=min_size))
            if post:
                alpha = alpha.filter(ImageFilter.MinFilter(size=3))
                alpha = alpha.filter(ImageFilter.MaxFilter(size=3))

    feather = int(cfg.get("alpha_edge_feather", 0) or 0)
    if feather > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=float(feather)))

    image.putalpha(alpha)
    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=1)
def _preferred_onnx_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except Exception:
        return ["CPUExecutionProvider"]

    # Prevent cuDNN DLL version conflicts between torch and onnxruntime on Windows.
    # If torch is imported first, onnxruntime.preload_dlls() keeps torch's CUDA/cuDNN
    # set and skips loading potentially incompatible nvidia-* wheel DLLs.
    try:
        import torch  # noqa: F401
    except Exception:
        pass

    try:
        preload = getattr(ort, "preload_dlls", None)
        if callable(preload):
            try:
                with _suppress_noisy_runtime_output():
                    preload(directory="")
            except TypeError:
                with _suppress_noisy_runtime_output():
                    preload()
            except Exception:
                pass
    except Exception:
        pass

    try:
        available = set(ort.get_available_providers() or [])
    except Exception:
        available = set()

    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@lru_cache(maxsize=4)
def _rembg_session(model_name: str):
    _configure_local_model_cache()
    try:
        from rembg import new_session
    except ImportError as exc:  # pragma: no cover - handled by callers in runtime only
        raise RuntimeError(
            "Background removal requires rembg. Install it in your GameManager env."
        ) from exc
    preferred = _preferred_onnx_providers()
    with _BACKGROUND_MODEL_LOCK:
        parked = _PARKED_BACKGROUND_MODELS.get(str(model_name))
    if parked is not None and "CUDAExecutionProvider" not in preferred:
        session = parked
    else:
        try:
            session = new_session(model_name, providers=preferred)
        except Exception:
            if preferred != ["CPUExecutionProvider"]:
                with _BACKGROUND_MODEL_LOCK:
                    parked = _PARKED_BACKGROUND_MODELS.get(str(model_name))
                if parked is not None:
                    session = parked
                else:
                    session = new_session(model_name, providers=["CPUExecutionProvider"])
            else:
                raise
    with _BACKGROUND_MODEL_LOCK:
        _ACTIVE_BACKGROUND_MODELS.add(str(model_name))
    return session


def _normalize_input_image_bytes(image_bytes: bytes) -> bytes:
    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGBA")
        out = BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return image_bytes


@contextmanager
def _filtered_removal_warnings():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=(
                "Palette images with Transparency expressed in bytes "
                "should be converted to RGBA images"
            ),
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=(
                r"invalid value encountered in scalar divide|"
                r"divide by zero encountered in scalar divide|"
                r"invalid value encountered in scalar multiply"
            ),
            category=RuntimeWarning,
            module=r"pymatting\.solver\.cg",
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*Thresholded incomplete Cholesky decomposition failed.*",
            category=UserWarning,
            module=r".*pymatting.*",
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*Thresholded incomplete Cholesky decomposition failed.*",
            category=RuntimeWarning,
            module=r".*pymatting.*",
        )
        yield


@contextmanager
def _suppress_noisy_runtime_output():
    buf_out = StringIO()
    buf_err = StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        yield


def remove_background_bytes(
    image_bytes: bytes,
    engine: str | None,
    *,
    params: dict[str, object] | None = None,
) -> bytes:
    mode = normalize_background_removal_engine(engine)
    if mode == "none":
        return image_bytes
    if mode == "pick_colors":
        return _remove_background_pick_colors(image_bytes, params=params)
    model = "u2net" if mode == "rembg" else "bria-rmbg"
    try:
        from rembg import remove
    except ImportError as exc:  # pragma: no cover - handled by callers in runtime only
        raise RuntimeError(
            "Background removal requires rembg. Install it in your GameManager env."
        ) from exc
    session = _rembg_session(model)
    remove_kwargs = _rembg_remove_kwargs(params)
    normalized_input = _normalize_input_image_bytes(image_bytes)
    try:
        with _filtered_removal_warnings():
            with _suppress_noisy_runtime_output():
                removed = remove(
                    normalized_input,
                    session=session,
                    force_return_bytes=True,
                    **remove_kwargs,
                )
            return _tune_cutout_alpha(removed, remove_kwargs)
    except TypeError:
        # Compatibility for older rembg signatures.
        with _filtered_removal_warnings():
            with _suppress_noisy_runtime_output():
                removed = remove(normalized_input, session=session, **remove_kwargs)
            return _tune_cutout_alpha(removed, remove_kwargs)


def preload_background_models() -> dict[str, str]:
    report: dict[str, str] = {}
    try:
        _preferred_onnx_providers()
        report["providers"] = "ok"
    except Exception as exc:
        report["providers"] = f"error: {exc}"
    for model_name in ("u2net", "bria-rmbg"):
        try:
            _rembg_session(model_name)
            report[model_name] = "ok"
        except Exception as exc:
            report[model_name] = f"error: {exc}"
    return report


def preload_background_engine(engine: str | None) -> str:
    mode = normalize_background_removal_engine(engine)
    if mode == "none":
        return "disabled"
    model_name = "u2net" if mode == "rembg" else "bria-rmbg"
    _rembg_session(model_name)
    return "ok"


def _park_background_model_to_ram(model_name: str) -> bool:
    model_token = str(model_name).strip()
    if not model_token:
        return False
    with _BACKGROUND_MODEL_LOCK:
        if model_token in _PARKED_BACKGROUND_MODELS:
            return True
    try:
        from rembg import new_session
    except Exception:
        return False
    try:
        session = new_session(model_token, providers=["CPUExecutionProvider"])
    except Exception:
        return False
    with _BACKGROUND_MODEL_LOCK:
        _PARKED_BACKGROUND_MODELS[model_token] = session
    return True


def clear_parked_background_models() -> int:
    with _BACKGROUND_MODEL_LOCK:
        count = len(_PARKED_BACKGROUND_MODELS)
        _PARKED_BACKGROUND_MODELS.clear()
    gc.collect()
    return count


def background_model_memory_state() -> dict[str, object]:
    providers = _preferred_onnx_providers()
    cache_info = _rembg_session.cache_info()
    with _BACKGROUND_MODEL_LOCK:
        loaded = sorted(_ACTIVE_BACKGROUND_MODELS)
        parked = sorted(_PARKED_BACKGROUND_MODELS.keys())
    return {
        "loaded_models": loaded,
        "parked_models": parked,
        "session_cache_currsize": int(cache_info.currsize),
        "session_cache_maxsize": int(cache_info.maxsize),
        "providers": providers,
        "cuda_preferred": "CUDAExecutionProvider" in providers,
    }


def release_background_models(
    *,
    clear_provider_cache: bool = False,
    aggressive: bool = False,
    park_in_ram: bool = True,
    drop_parked: bool = False,
) -> dict[str, object]:
    with _BACKGROUND_MODEL_LOCK:
        released_models = sorted(_ACTIVE_BACKGROUND_MODELS)
        _ACTIVE_BACKGROUND_MODELS.clear()
    parked_now: list[str] = []
    if park_in_ram:
        for model_name in released_models:
            if _park_background_model_to_ram(model_name):
                parked_now.append(str(model_name))
    _rembg_session.cache_clear()
    if drop_parked:
        clear_parked_background_models()
    if clear_provider_cache or aggressive:
        _preferred_onnx_providers.cache_clear()
    if aggressive:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    gc.collect()
    return {
        "released_models": released_models,
        "released_count": len(released_models),
        "parked_models": sorted(set(parked_now)),
        "parked_count": len(set(parked_now)),
        "park_in_ram": bool(park_in_ram),
        "drop_parked": bool(drop_parked),
        "aggressive": bool(aggressive),
    }
