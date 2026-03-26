from __future__ import annotations

from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import colorsys
from dataclasses import dataclass
import filecmp
from functools import lru_cache
import gc
from io import BytesIO, StringIO
import json
import logging
import os
import re
import shutil
from pathlib import Path
import threading
from typing import Final

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from gamemanager.services.background_removal import (
    DEFAULT_BG_REMOVAL_PARAMS,
    normalize_background_removal_engine,
    normalize_background_removal_params,
    remove_background_bytes,
)
from gamemanager.services.paths import project_data_dir, project_root

# Keep PaddleX/PaddleOCR local-only by default, no startup host probing.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLEOCR_LOG_LEVEL", "ERROR")
os.environ.setdefault("PPOCR_LOG_LEVEL", "ERROR")


ICO_SIZES: Final[list[int]] = [256, 128, 64, 48, 32, 24, 16]
PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parents[1]
PROJECT_ROOT: Final[Path] = project_root()
BUILTIN_TEMPLATE_DIR: Final[Path] = PACKAGE_DIR / "templates"
CUSTOM_TEMPLATE_DIR: Final[Path] = PROJECT_ROOT / "IconTemplates"
ROUND_TEMPLATE_PATHS: Final[list[Path]] = [
    PACKAGE_DIR / "IconTemplate.png",
    PACKAGE_DIR / "RoundTemplate.png",
]
SQUARE_TEMPLATE_PATHS: Final[list[Path]] = [
    PACKAGE_DIR / "SquareTemplate.png",
    PACKAGE_DIR / "SquareTemplace.png",
]


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


def _configure_local_ml_model_cache() -> None:
    model_root = project_data_dir() / "models"
    model_root.mkdir(parents=True, exist_ok=True)
    paddleocr_root = model_root / "paddleocr"
    paddle_root = model_root / "paddle"
    xdg_root = model_root / "xdg"
    paddleocr_root.mkdir(parents=True, exist_ok=True)
    paddle_root.mkdir(parents=True, exist_ok=True)
    xdg_root.mkdir(parents=True, exist_ok=True)

    # Migrate legacy user-profile model stores into project-local data.
    home = Path.home()
    _merge_move_dir(home / ".paddleocr", paddleocr_root)
    _merge_move_dir(home / ".cache" / "paddle", paddle_root)

    os.environ.setdefault("PADDLE_OCR_BASE_DIR", str(paddleocr_root))
    os.environ.setdefault("PADDLE_HOME", str(paddle_root))
    os.environ.setdefault("XDG_CACHE_HOME", str(xdg_root))


_configure_local_ml_model_cache()


@dataclass(frozen=True, slots=True)
class IconTemplate:
    template_id: str
    label: str
    shape: str | None
    path: Path | None


@dataclass(frozen=True, slots=True)
class TemplateAnalysis:
    overlay: Image.Image
    interior_mask: Image.Image | None
    shape: str | None


@dataclass(frozen=True, slots=True)
class BorderShaderConfig:
    enabled: bool = False
    mode: str = "hsv"
    hue: int = 0
    saturation: int = 100
    tone: int = 100
    intensity: int = 0


@dataclass(frozen=True, slots=True)
class TextPreserveConfig:
    enabled: bool = False
    strength: int = 45
    feather: int = 1
    method: str = "none"
    color_groups: int = 4
    include_outline: bool = True
    include_shadow: bool = True
    glow_mode: str = "disabled"
    glow_radius: int = 2
    glow_strength: int = 40
    roi: tuple[float, float, float, float] | None = None
    seed_colors: tuple[tuple[int, int, int], ...] = ()
    seed_tolerance: int = 26
    manual_add_seeds: tuple[tuple[float, float], ...] = ()
    manual_remove_seeds: tuple[tuple[float, float], ...] = ()


TEXT_EXTRACTION_METHOD_OPTIONS: Final[list[tuple[str, str]]] = [
    ("Disabled", "none"),
    ("ROI Guided", "roi_guided"),
    ("PaddleOCR", "paddleocr"),
    ("OpenCV DB", "opencv_db"),
    ("Heuristic", "heuristic"),
]

_TEXT_EXTRACTION_RUNTIME_STATUS: dict[str, str] = {}
_TEXT_MODEL_LOCK = threading.Lock()
_ACTIVE_TEXT_MODELS: set[str] = set()
_PARKED_PADDLEOCR_ENGINE: object | None = None
_TEMPLATE_MAP_LOCK = threading.Lock()
_TEMPLATE_MAP_CACHE: dict[str, "IconTemplate"] | None = None
_TEMPLATE_MAP_CACHE_KEY: tuple[int, int] | None = None


def _silence_paddle_text_logs() -> None:
    for name in ("ppocr", "paddleocr", "paddle", "paddlex"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.ERROR)
        logger.propagate = False


def _suppressed_runtime_output():
    out = StringIO()
    err = StringIO()
    return redirect_stdout(out), redirect_stderr(err)


def _import_paddleocr_runtime():
    # Workaround on Windows: importing torch first avoids sporadic DLL resolution
    # failures when PaddleOCR transitively imports albumentations->torch.
    _silence_paddle_text_logs()
    try:
        import torch  # noqa: F401
    except Exception:
        pass
    stdout_ctx, stderr_ctx = _suppressed_runtime_output()
    with stdout_ctx, stderr_ctx:
        from paddleocr import PaddleOCR  # type: ignore

    return PaddleOCR


def _crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _apply_size_profile(image: Image.Image, size: int) -> Image.Image:
    # Stronger local punch for tiny sizes where details collapse.
    if size <= 24:
        contrast, saturation, sharpness, brightness = 1.28, 1.12, 1.45, 1.05
    elif size <= 48:
        contrast, saturation, sharpness, brightness = 1.18, 1.08, 1.25, 1.02
    else:
        contrast, saturation, sharpness, brightness = 1.08, 1.04, 1.12, 1.0
    image = ImageEnhance.Contrast(image).enhance(contrast)
    image = ImageEnhance.Color(image).enhance(saturation)
    image = ImageEnhance.Sharpness(image).enhance(sharpness)
    image = ImageEnhance.Brightness(image).enhance(brightness)
    return image


def _apply_circle_and_ring(image: Image.Image, size: int) -> Image.Image:
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = 1 if size <= 24 else 2
    draw.ellipse((inset, inset, size - inset - 1, size - inset - 1), fill=255)
    image.putalpha(mask)

    ring_thickness = 0
    if size >= 128:
        ring_thickness = 12
    elif size >= 64:
        ring_thickness = 5
    elif size >= 32:
        ring_thickness = 2
    elif size >= 24:
        ring_thickness = 1
    if ring_thickness <= 0:
        return image

    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring)
    outer = max(inset, ring_thickness // 2)
    ring_box = (outer, outer, size - outer - 1, size - outer - 1)

    # Metallic look: multi-tone ring body + highlight and shadow arcs.
    for idx in range(ring_thickness):
        t = idx / max(1, ring_thickness - 1)
        brightness = int(146 + (1.0 - abs((2.0 * t) - 1.0)) * 90.0)
        color = (brightness, brightness, min(255, brightness + 8), 225)
        box = (
            ring_box[0] + idx * 0.5,
            ring_box[1] + idx * 0.5,
            ring_box[2] - idx * 0.5,
            ring_box[3] - idx * 0.5,
        )
        ring_draw.ellipse(box, outline=color, width=1)

    highlight_width = max(1, ring_thickness // 3)
    ring_draw.arc(
        ring_box,
        start=34,
        end=145,
        fill=(255, 255, 255, 190),
        width=highlight_width,
    )
    shadow_width = max(1, ring_thickness // 3)
    ring_draw.arc(
        ring_box,
        start=205,
        end=320,
        fill=(52, 52, 58, 165),
        width=shadow_width,
    )
    return Image.alpha_composite(image, ring)


def _draw_square_fallback_overlay(size: int) -> Image.Image:
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ring)
    margin = max(2, int(size * 0.02))
    border = max(2, int(size * 0.03))
    radius = max(6, int(size * 0.10))
    outer = (margin, margin, size - margin - 1, size - margin - 1)
    draw.rounded_rectangle(
        outer,
        radius=radius,
        outline=(190, 190, 196, 215),
        width=border,
    )
    return ring


def _slugify_template_id(raw_value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", raw_value.strip().casefold()).strip("_")
    return token or "template"


def _label_from_stem(stem: str) -> str:
    token = stem.strip()
    if not token:
        return "Template"
    return token


def _shape_from_name(stem: str) -> str | None:
    lowered = stem.casefold()
    if any(token in lowered for token in ("round", "circle", "ring", "orb")):
        return "round"
    if any(token in lowered for token in ("square", "rect", "box", "frame")):
        return "square"
    return None


def _build_default_templates() -> dict[str, IconTemplate]:
    round_path = next((p for p in ROUND_TEMPLATE_PATHS if p.exists()), None)
    square_path = next((p for p in SQUARE_TEMPLATE_PATHS if p.exists()), None)
    templates: dict[str, IconTemplate] = {
        "none": IconTemplate("none", "Disabled", None, None),
    }
    if round_path is not None:
        templates["round"] = IconTemplate("round", "Round Border", "round", round_path)
    if square_path is not None:
        templates["square"] = IconTemplate("square", "Square Border", "square", square_path)
    return templates


def _parse_template_metadata(template_path: Path) -> tuple[str | None, str | None]:
    meta_path = template_path.with_suffix(".json")
    if not meta_path.exists():
        return None, None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    raw_shape = str(payload.get("shape", "")).strip().casefold()
    shape = raw_shape if raw_shape in {"round", "square"} else None
    label = str(payload.get("label", "")).strip() or None
    return shape, label


def _discover_custom_templates() -> list[IconTemplate]:
    CUSTOM_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    return _discover_templates_from_dir(CUSTOM_TEMPLATE_DIR)


def _discover_templates_from_dir(template_dir: Path) -> list[IconTemplate]:
    if not template_dir.exists():
        return []
    discovered: list[IconTemplate] = []
    for path in sorted(template_dir.glob("*.png"), key=lambda p: p.name.casefold()):
        shape_meta, _label_meta = _parse_template_metadata(path)
        shape = shape_meta or _shape_from_name(path.stem) or "square"
        template_id = _slugify_template_id(path.stem)
        label = _label_from_stem(path.stem)
        discovered.append(
            IconTemplate(
                template_id=template_id,
                label=label,
                shape=shape,
                path=path,
            )
        )
    return discovered


def _discover_builtin_templates() -> list[IconTemplate]:
    return _discover_templates_from_dir(BUILTIN_TEMPLATE_DIR)


def _insert_template_entry(mapping: dict[str, IconTemplate], template: IconTemplate) -> None:
    key = template.template_id
    if key in mapping:
        idx = 2
        while f"{key}_{idx}" in mapping:
            idx += 1
        key = f"{key}_{idx}"
    mapping[key] = IconTemplate(
        template_id=key,
        label=template.label,
        shape=template.shape,
        path=template.path,
    )


def _dir_mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return -1


def _icon_template_map() -> dict[str, IconTemplate]:
    global _TEMPLATE_MAP_CACHE, _TEMPLATE_MAP_CACHE_KEY
    cache_key = (_dir_mtime_ns(BUILTIN_TEMPLATE_DIR), _dir_mtime_ns(CUSTOM_TEMPLATE_DIR))
    with _TEMPLATE_MAP_LOCK:
        if _TEMPLATE_MAP_CACHE is not None and _TEMPLATE_MAP_CACHE_KEY == cache_key:
            return _TEMPLATE_MAP_CACHE
        mapping = _build_default_templates()
        for template in _discover_builtin_templates():
            _insert_template_entry(mapping, template)
        for template in _discover_custom_templates():
            _insert_template_entry(mapping, template)
        _TEMPLATE_MAP_CACHE = mapping
        _TEMPLATE_MAP_CACHE_KEY = cache_key
        return mapping


def icon_style_options() -> list[tuple[str, str]]:
    mapping = _icon_template_map()
    entries = [mapping["none"]]
    custom = [tpl for key, tpl in mapping.items() if key != "none"]
    custom.sort(
        key=lambda tpl: (
            0 if tpl.template_id == "round" else 1 if tpl.template_id == "square" else 2,
            tpl.label.casefold(),
            tpl.template_id.casefold(),
        )
    )
    entries.extend(custom)
    return [(tpl.label, tpl.template_id) for tpl in entries]


def normalize_icon_style(
    icon_style: str | None,
    circular_ring: bool | None = None,
) -> str:
    style_map = _icon_template_map()
    if icon_style:
        style = icon_style.strip().casefold()
        if style in style_map:
            return style
        if style in {"round", "circle", "ring"}:
            round_like = sorted(
                (
                    key
                    for key, tpl in style_map.items()
                    if key != "none" and str(tpl.shape or "").casefold() == "round"
                ),
                key=str.casefold,
            )
            if round_like:
                return round_like[0]
        if style in {"square", "rect", "rectangle"}:
            square_like = sorted(
                (
                    key
                    for key, tpl in style_map.items()
                    if key != "none" and str(tpl.shape or "").casefold() == "square"
                ),
                key=str.casefold,
            )
            if square_like:
                return square_like[0]
    if circular_ring is None:
        return "none"
    if not circular_ring:
        return "none"
    if "round" in style_map:
        return "round"
    round_like = sorted(
        (
            key
            for key, tpl in style_map.items()
            if key != "none" and str(tpl.shape or "").casefold() == "round"
        ),
        key=str.casefold,
    )
    return round_like[0] if round_like else "none"


def resolve_icon_template(
    icon_style: str | None,
    circular_ring: bool | None = None,
) -> IconTemplate:
    mapping = _icon_template_map()
    style = normalize_icon_style(icon_style, circular_ring)
    return mapping.get(style, mapping["none"])


def normalize_border_shader_config(
    config: BorderShaderConfig | dict[str, object] | None,
) -> BorderShaderConfig:
    if config is None:
        return BorderShaderConfig()
    if isinstance(config, BorderShaderConfig):
        raw = config
    elif isinstance(config, dict):
        raw = BorderShaderConfig(
            enabled=bool(config.get("enabled", False)),
            mode=str(config.get("mode", "hsv")).strip().casefold() or "hsv",
            hue=int(config.get("hue", 0) or 0),
            saturation=int(config.get("saturation", 100) or 100),
            tone=int(config.get("tone", 100) or 100),
            intensity=int(config.get("intensity", 0) or 0),
        )
    else:
        return BorderShaderConfig()
    mode = raw.mode if raw.mode in {"hsv", "hsl"} else "hsv"
    return BorderShaderConfig(
        enabled=bool(raw.enabled),
        mode=mode,
        hue=max(0, min(359, int(raw.hue))),
        saturation=max(0, min(100, int(raw.saturation))),
        tone=max(0, min(100, int(raw.tone))),
        intensity=max(0, min(100, int(raw.intensity))),
    )


def border_shader_to_dict(
    config: BorderShaderConfig | dict[str, object] | None,
) -> dict[str, object]:
    normalized = normalize_border_shader_config(config)
    return {
        "enabled": bool(normalized.enabled),
        "mode": normalized.mode,
        "hue": int(normalized.hue),
        "saturation": int(normalized.saturation),
        "tone": int(normalized.tone),
        "intensity": int(normalized.intensity),
    }


def normalize_text_extraction_method(
    method: str | None,
    *,
    enabled_fallback: bool = False,
) -> str:
    value = (method or "").strip().casefold()
    valid = {option for _, option in TEXT_EXTRACTION_METHOD_OPTIONS}
    if value in valid:
        return value
    return "heuristic" if enabled_fallback else "none"


def _normalize_glow_mode(mode: str | None) -> str:
    value = (mode or "").strip().casefold()
    if value in {"disabled", "bright", "dark", "both"}:
        return value
    return "disabled"


def _normalize_text_roi(value: object | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    x = y = w = h = None
    if isinstance(value, dict):
        try:
            x = float(value.get("x", value.get("left", 0.0)))
            y = float(value.get("y", value.get("top", 0.0)))
            w = float(value.get("w", value.get("width", 0.0)))
            h = float(value.get("h", value.get("height", 0.0)))
        except (TypeError, ValueError):
            return None
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            x = float(value[0])
            y = float(value[1])
            w = float(value[2])
            h = float(value[3])
        except (TypeError, ValueError):
            return None
    if x is None or y is None or w is None or h is None:
        return None
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    w = max(0.0, min(1.0 - x, w))
    h = max(0.0, min(1.0 - y, h))
    if w <= 0.0 or h <= 0.0:
        return None
    return (x, y, w, h)


def _normalize_seed_colors(
    value: object | None,
    *,
    max_items: int,
) -> tuple[tuple[int, int, int], ...]:
    if max_items <= 0:
        return ()
    if value is None:
        return ()
    items: list[tuple[int, int, int]] = []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = [value]
    for raw in raw_items:
        rgb: tuple[int, int, int] | None = None
        if isinstance(raw, dict):
            try:
                red = int(raw.get("r"))
                green = int(raw.get("g"))
                blue = int(raw.get("b"))
                rgb = (red, green, blue)
            except (TypeError, ValueError):
                rgb = None
        elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
            try:
                rgb = (int(raw[0]), int(raw[1]), int(raw[2]))
            except (TypeError, ValueError):
                rgb = None
        if rgb is None:
            continue
        red, green, blue = rgb
        clamped = (
            max(0, min(255, red)),
            max(0, min(255, green)),
            max(0, min(255, blue)),
        )
        if clamped not in items:
            items.append(clamped)
        if len(items) >= max_items:
            break
    return tuple(items)


def _normalize_manual_seed_points(
    value: object | None,
    *,
    max_items: int = 512,
) -> tuple[tuple[float, float], ...]:
    if value is None or max_items <= 0:
        return ()
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = [value]
    points: list[tuple[float, float]] = []
    for raw in raw_items:
        x_val: float | None = None
        y_val: float | None = None
        if isinstance(raw, dict):
            try:
                x_val = float(raw.get("x"))
                y_val = float(raw.get("y"))
            except (TypeError, ValueError):
                x_val = None
                y_val = None
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                x_val = float(raw[0])
                y_val = float(raw[1])
            except (TypeError, ValueError):
                x_val = None
                y_val = None
        if x_val is None or y_val is None:
            continue
        x_val = max(0.0, min(1.0, x_val))
        y_val = max(0.0, min(1.0, y_val))
        point = (x_val, y_val)
        if point in points:
            continue
        points.append(point)
        if len(points) >= max_items:
            break
    return tuple(points)


def _set_text_runtime_status(method: str, status: str) -> None:
    _TEXT_EXTRACTION_RUNTIME_STATUS[method] = status


def _paddle_can_use_cuda() -> bool:
    try:
        import paddle
    except Exception:
        return False
    try:
        compiled = bool(getattr(paddle, "is_compiled_with_cuda", lambda: False)())
    except Exception:
        compiled = False
    if not compiled:
        return False
    try:
        count = int(getattr(paddle.device.cuda, "device_count", lambda: 0)())
    except Exception:
        count = 0
    return count > 0


def _dll_on_path(filename: str) -> bool:
    path_raw = os.environ.get("PATH", "")
    if not path_raw:
        return False
    for entry in path_raw.split(os.pathsep):
        token = entry.strip().strip('"')
        if not token:
            continue
        try:
            candidate = Path(token) / filename
        except Exception:
            continue
        if candidate.exists():
            return True
    return False


def _paddle_gpu_runtime_ready() -> bool:
    if not _paddle_can_use_cuda():
        return False
    # Paddle GPU 2.6.x on Windows expects cuDNN 8 runtime naming.
    return _dll_on_path("cudnn64_8.dll")


def _opencv_dnn_cuda_available() -> bool:
    try:
        import cv2
    except Exception:
        return False
    if not (
        hasattr(cv2.dnn, "DNN_BACKEND_CUDA")
        and (hasattr(cv2.dnn, "DNN_TARGET_CUDA") or hasattr(cv2.dnn, "DNN_TARGET_CUDA_FP16"))
    ):
        return False
    try:
        if hasattr(cv2, "cuda") and hasattr(cv2.cuda, "getCudaEnabledDeviceCount"):
            return int(cv2.cuda.getCudaEnabledDeviceCount()) > 0
    except Exception:
        return False
    return False


def text_extraction_device_status(method: str | None) -> str:
    normalized = normalize_text_extraction_method(method)
    if normalized == "none":
        return "Disabled"
    if normalized == "heuristic":
        return "CPU"
    if normalized == "roi_guided":
        return "CPU"
    runtime = _TEXT_EXTRACTION_RUNTIME_STATUS.get(normalized)
    if runtime:
        return runtime
    if normalized == "paddleocr":
        try:
            _import_paddleocr_runtime()
        except ImportError:
            return "Unavailable (paddleocr missing)"
        except Exception:
            return "Unavailable (paddleocr runtime error)"
        if _paddle_gpu_runtime_ready():
            return "Ready (GPU preferred)"
        if _paddle_can_use_cuda():
            return "Ready (CPU fallback: missing cuDNN8 runtime)"
        return "Ready (CPU)"
    if normalized == "opencv_db":
        try:
            import cv2  # noqa: F401
        except Exception:
            return "Unavailable (opencv missing)"
        try:
            _opencv_db_model_path()
        except RuntimeError:
            return "Unavailable (DB model missing)"
        return "Ready (GPU preferred)" if _opencv_dnn_cuda_available() else "Ready (CPU)"
    return "Unknown"


def normalize_text_preserve_config(
    config: TextPreserveConfig | dict[str, object] | None,
) -> TextPreserveConfig:
    if config is None:
        return TextPreserveConfig()
    if isinstance(config, TextPreserveConfig):
        raw_enabled = bool(config.enabled)
        raw_strength = int(config.strength)
        raw_feather = int(config.feather)
        raw_method = str(config.method)
        raw_color_groups = int(config.color_groups)
        raw_include_outline = bool(config.include_outline)
        raw_include_shadow = bool(config.include_shadow)
        raw_glow_mode = str(config.glow_mode)
        raw_glow_radius = int(config.glow_radius)
        raw_glow_strength = int(config.glow_strength)
        raw_roi = config.roi
        raw_seed_colors = config.seed_colors
        raw_seed_tolerance = int(config.seed_tolerance)
        raw_manual_add_seeds = config.manual_add_seeds
        raw_manual_remove_seeds = config.manual_remove_seeds
    elif isinstance(config, dict):
        raw_enabled = bool(config.get("enabled", False))
        raw_strength = int(config.get("strength", 45) or 45)
        raw_feather = int(config.get("feather", 1) or 1)
        raw_method = str(config.get("method", "") or "")
        raw_color_groups = int(config.get("color_groups", 4) or 4)
        raw_include_outline = bool(config.get("include_outline", True))
        raw_include_shadow = bool(config.get("include_shadow", True))
        raw_glow_mode = str(config.get("glow_mode", "disabled") or "disabled")
        raw_glow_radius = int(config.get("glow_radius", 2) or 2)
        raw_glow_strength = int(config.get("glow_strength", 40) or 40)
        raw_roi = config.get("roi")
        raw_seed_colors = config.get("seed_colors")
        raw_seed_tolerance = int(config.get("seed_tolerance", 26) or 26)
        raw_manual_add_seeds = config.get("manual_add_seeds")
        raw_manual_remove_seeds = config.get("manual_remove_seeds")
    else:
        return TextPreserveConfig()
    method = normalize_text_extraction_method(raw_method, enabled_fallback=raw_enabled)
    roi = _normalize_text_roi(raw_roi)
    color_groups = max(2, min(8, raw_color_groups))
    seed_colors = _normalize_seed_colors(raw_seed_colors, max_items=color_groups)
    manual_add_seeds = _normalize_manual_seed_points(raw_manual_add_seeds)
    manual_remove_seeds = _normalize_manual_seed_points(raw_manual_remove_seeds)
    enabled = (method != "none") or bool(manual_add_seeds) or bool(manual_remove_seeds)
    return TextPreserveConfig(
        enabled=enabled,
        strength=max(0, min(100, raw_strength)),
        feather=max(0, min(3, raw_feather)),
        method=method,
        color_groups=color_groups,
        include_outline=raw_include_outline,
        include_shadow=raw_include_shadow,
        glow_mode=_normalize_glow_mode(raw_glow_mode),
        glow_radius=max(0, min(12, raw_glow_radius)),
        glow_strength=max(0, min(100, raw_glow_strength)),
        roi=roi,
        seed_colors=seed_colors,
        seed_tolerance=max(4, min(96, raw_seed_tolerance)),
        manual_add_seeds=manual_add_seeds,
        manual_remove_seeds=manual_remove_seeds,
    )


def text_preserve_to_dict(
    config: TextPreserveConfig | dict[str, object] | None,
) -> dict[str, object]:
    normalized = normalize_text_preserve_config(config)
    return {
        "enabled": normalized.enabled,
        "strength": normalized.strength,
        "feather": normalized.feather,
        "method": normalized.method,
        "color_groups": normalized.color_groups,
        "include_outline": normalized.include_outline,
        "include_shadow": normalized.include_shadow,
        "glow_mode": normalized.glow_mode,
        "glow_radius": normalized.glow_radius,
        "glow_strength": normalized.glow_strength,
        "roi": list(normalized.roi) if normalized.roi is not None else None,
        "seed_colors": [list(color) for color in normalized.seed_colors],
        "seed_tolerance": normalized.seed_tolerance,
        "manual_add_seeds": [[point[0], point[1]] for point in normalized.manual_add_seeds],
        "manual_remove_seeds": [[point[0], point[1]] for point in normalized.manual_remove_seeds],
    }


def _confidence_threshold_from_strength(strength: int) -> float:
    # Lower threshold for higher strengths so weaker/low-contrast text is kept.
    return max(0.05, min(0.8, 0.65 - (strength * 0.005)))


def _polygon_from_points(raw_points) -> list[tuple[float, float]] | None:
    if not isinstance(raw_points, (list, tuple)):
        return None
    points: list[tuple[float, float]] = []
    for point in raw_points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            x = float(point[0])
            y = float(point[1])
        except (TypeError, ValueError):
            continue
        points.append((x, y))
    if len(points) < 3:
        return None
    return points


def _draw_polygon_mask(
    size: tuple[int, int],
    polygons: list[list[tuple[float, float]]],
) -> Image.Image:
    mask = Image.new("L", size, 0)
    if not polygons:
        return mask
    draw = ImageDraw.Draw(mask)
    for polygon in polygons:
        draw.polygon(polygon, fill=255)
    return mask


@lru_cache(maxsize=1)
def _paddleocr_engine():
    try:
        PaddleOCR = _import_paddleocr_runtime()
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR text extraction requires paddleocr. "
            "Install it in the GameManager environment."
        ) from exc
    use_gpu = _paddle_gpu_runtime_ready()
    # Reuse parked CPU engine when GPU path is unavailable.
    if not use_gpu:
        with _TEXT_MODEL_LOCK:
            parked = _PARKED_PADDLEOCR_ENGINE
        if parked is not None:
            with _TEXT_MODEL_LOCK:
                _ACTIVE_TEXT_MODELS.add("paddleocr")
            return parked
    # Keep detection-only mode for speed and to avoid OCR text decode costs.
    # Auto-fallback to CPU when Paddle GPU runtime DLLs are not available.
    _silence_paddle_text_logs()
    stdout_ctx, stderr_ctx = _suppressed_runtime_output()
    with stdout_ctx, stderr_ctx:
        engine = PaddleOCR(
            use_angle_cls=False,
            lang="en",
            det=True,
            rec=False,
            show_log=False,
            use_gpu=use_gpu,
        )
    with _TEXT_MODEL_LOCK:
        _ACTIVE_TEXT_MODELS.add("paddleocr")
    return engine


def preload_text_models() -> dict[str, str]:
    report: dict[str, str] = {}
    try:
        _paddleocr_engine()
        report["paddleocr"] = "ok"
    except Exception as exc:
        report["paddleocr"] = f"error: {exc}"
    try:
        model_path = _opencv_db_model_path()
        _opencv_db_detector(str(model_path))
        report["opencv_db"] = "ok"
    except Exception as exc:
        report["opencv_db"] = f"error: {exc}"
    return report


def preload_text_model(method: str | None) -> str:
    normalized = normalize_text_extraction_method(method)
    if normalized in {"none", "heuristic", "roi_guided"}:
        return "disabled"
    if normalized == "paddleocr":
        _paddleocr_engine()
        return "ok"
    if normalized == "opencv_db":
        model_path = _opencv_db_model_path()
        _opencv_db_detector(str(model_path))
        return "ok"
    return "disabled"


def _park_paddleocr_to_ram() -> bool:
    global _PARKED_PADDLEOCR_ENGINE
    with _TEXT_MODEL_LOCK:
        if _PARKED_PADDLEOCR_ENGINE is not None:
            return True
    try:
        PaddleOCR = _import_paddleocr_runtime()
        _silence_paddle_text_logs()
        stdout_ctx, stderr_ctx = _suppressed_runtime_output()
        with stdout_ctx, stderr_ctx:
            engine = PaddleOCR(
                use_angle_cls=False,
                lang="en",
                det=True,
                rec=False,
                show_log=False,
                use_gpu=False,
            )
    except Exception:
        return False
    with _TEXT_MODEL_LOCK:
        _PARKED_PADDLEOCR_ENGINE = engine
    return True


def clear_parked_text_models() -> int:
    global _PARKED_PADDLEOCR_ENGINE
    with _TEXT_MODEL_LOCK:
        count = 1 if _PARKED_PADDLEOCR_ENGINE is not None else 0
        _PARKED_PADDLEOCR_ENGINE = None
    gc.collect()
    return count


def text_model_memory_state() -> dict[str, object]:
    with _TEXT_MODEL_LOCK:
        loaded = sorted(_ACTIVE_TEXT_MODELS)
        parked_paddle = _PARKED_PADDLEOCR_ENGINE is not None
    paddle_cache = _paddleocr_engine.cache_info()
    opencv_cache = _opencv_db_detector.cache_info()
    return {
        "loaded_models": loaded,
        "parked_models": ["paddleocr"] if parked_paddle else [],
        "paddle_cache_currsize": int(paddle_cache.currsize),
        "opencv_cache_currsize": int(opencv_cache.currsize),
        "paddle_status": text_extraction_device_status("paddleocr"),
        "opencv_status": text_extraction_device_status("opencv_db"),
    }


def release_text_models(
    *,
    aggressive: bool = False,
    clear_runtime_status: bool = False,
    include_template_cache: bool = False,
    park_in_ram: bool = True,
    drop_parked: bool = False,
) -> dict[str, object]:
    with _TEXT_MODEL_LOCK:
        released = sorted(_ACTIVE_TEXT_MODELS)
        _ACTIVE_TEXT_MODELS.clear()
    parked_now: list[str] = []
    if park_in_ram and "paddleocr" in released:
        if _park_paddleocr_to_ram():
            parked_now.append("paddleocr")
    _paddleocr_engine.cache_clear()
    _opencv_db_detector.cache_clear()
    if include_template_cache:
        _load_template_analysis.cache_clear()
    if drop_parked:
        clear_parked_text_models()
    if clear_runtime_status:
        _TEXT_EXTRACTION_RUNTIME_STATUS.clear()
    if aggressive:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    gc.collect()
    return {
        "released_models": released,
        "released_count": len(released),
        "parked_models": parked_now,
        "parked_count": len(parked_now),
        "park_in_ram": bool(park_in_ram),
        "drop_parked": bool(drop_parked),
        "aggressive": bool(aggressive),
        "include_template_cache": bool(include_template_cache),
    }


def _detect_text_mask_paddleocr(
    rgb: Image.Image,
    strength: int,
) -> Image.Image:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("PaddleOCR extraction requires numpy.") from exc
    engine = _paddleocr_engine()
    image_np = np.array(rgb)
    result = engine.ocr(image_np, det=True, rec=False, cls=False)
    entries = []
    if isinstance(result, list):
        if len(result) == 1 and isinstance(result[0], list):
            entries = result[0]
        else:
            entries = result
    conf_threshold = _confidence_threshold_from_strength(strength)
    polygons: list[list[tuple[float, float]]] = []
    for entry in entries:
        score: float | None = None
        points = None
        if isinstance(entry, dict):
            points = entry.get("points") or entry.get("polygon") or entry.get("poly")
            raw_score = entry.get("score")
            if isinstance(raw_score, (int, float)):
                score = float(raw_score)
        elif isinstance(entry, (list, tuple)) and entry:
            points = entry[0]
            if len(entry) > 1:
                score_block = entry[1]
                if isinstance(score_block, (int, float)):
                    score = float(score_block)
                elif isinstance(score_block, (list, tuple)) and score_block:
                    tail = score_block[-1]
                    if isinstance(tail, (int, float)):
                        score = float(tail)
        polygon = _polygon_from_points(points)
        if polygon is None:
            continue
        if score is not None and score < conf_threshold:
            continue
        polygons.append(polygon)
    return _draw_polygon_mask(rgb.size, polygons)


def _opencv_db_model_path() -> Path:
    env_path = os.environ.get("GAMEMANAGER_OPENCV_DB_MODEL", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.exists():
            return candidate
    candidates = [
        PROJECT_ROOT / ".gamemanager_data" / "models" / "opencv_db" / "DB_TD500_resnet18.onnx",
        PROJECT_ROOT / ".gamemanager_data" / "models" / "opencv_db" / "DB_TD500_resnet50.onnx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "OpenCV DB text extraction requires a DB model file. "
        "Set GAMEMANAGER_OPENCV_DB_MODEL or place DB_TD500_resnet18.onnx under "
        ".gamemanager_data/models/opencv_db/."
    )


@lru_cache(maxsize=1)
def _opencv_db_detector(model_path_text: str):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV DB text extraction requires opencv-python.") from exc
    detector = cv2.dnn_TextDetectionModel_DB(model_path_text)
    detector.setBinaryThreshold(0.3)
    detector.setPolygonThreshold(0.5)
    detector.setInputParams(
        1.0,
        (736, 736),
        (122.67891434, 116.66876762, 104.00698793),
        True,
    )
    with _TEXT_MODEL_LOCK:
        _ACTIVE_TEXT_MODELS.add("opencv_db")
    return detector


def _detect_text_mask_opencv_db(
    rgb: Image.Image,
    strength: int,
) -> Image.Image:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV DB text extraction requires opencv-python and numpy."
        ) from exc
    model_path = _opencv_db_model_path()
    detector = _opencv_db_detector(str(model_path))
    conf_threshold = _confidence_threshold_from_strength(strength)
    try:
        detector.setConfidenceThreshold(conf_threshold)
    except Exception:
        pass
    image_bgr = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
    detected = detector.detect(image_bgr)
    boxes = None
    scores = None
    if isinstance(detected, tuple):
        if len(detected) >= 1:
            boxes = detected[0]
        if len(detected) >= 2:
            scores = detected[1]
    else:
        boxes = detected
    polygons: list[list[tuple[float, float]]] = []
    if boxes is None:
        return _draw_polygon_mask(rgb.size, polygons)
    for idx, box in enumerate(boxes):
        if scores is not None and idx < len(scores):
            try:
                score = float(scores[idx])
            except (TypeError, ValueError):
                score = None
            if score is not None and score < conf_threshold:
                continue
        polygon = _polygon_from_points(box)
        if polygon is None:
            continue
        polygons.append(polygon)
    return _draw_polygon_mask(rgb.size, polygons)


def _detected_text_mask(
    rgb: Image.Image,
    method: str,
    strength: int,
) -> Image.Image:
    if method == "paddleocr":
        return _detect_text_mask_paddleocr(rgb, strength)
    if method == "opencv_db":
        return _detect_text_mask_opencv_db(rgb, strength)
    raise RuntimeError(f"Unsupported text extraction method: {method}")


def _heuristic_text_candidate_mask(
    rgb: Image.Image,
    alpha: Image.Image,
    cfg: TextPreserveConfig,
) -> Image.Image:
    gray = rgb.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    detail = ImageChops.difference(
        gray,
        gray.filter(ImageFilter.GaussianBlur(radius=1.0 + (cfg.strength / 60.0))),
    )
    edge_p90 = _hist_percentile(edges, 90.0)
    detail_p85 = _hist_percentile(detail, 85.0)
    edge_threshold = max(
        26,
        min(
            238,
            int(
                0.60 * edge_p90
                + 0.40 * max(45, min(220, int(178 - (cfg.strength * 1.08))))
            ),
        ),
    )
    detail_threshold = max(
        6,
        min(
            140,
            int(
                0.65 * detail_p85
                + 0.35 * max(12, min(120, int(70 - (cfg.strength * 0.38))))
            ),
        ),
    )
    edges_mask = edges.point(lambda v: 255 if int(v) >= edge_threshold else 0, mode="L")
    detail_mask = detail.point(
        lambda v: 255 if int(v) >= detail_threshold else 0,
        mode="L",
    )
    stroke_mask = ImageChops.multiply(edges_mask, detail_mask)

    hsv = rgb.convert("HSV")
    _h, s, v = hsv.split()
    v_hi = _hist_percentile(v, 72.0)
    s_lo = _hist_percentile(s, 40.0)
    lum_threshold = max(
        92,
        min(
            250,
            int(
                0.55 * v_hi
                + 0.45 * max(120, min(245, int(190 - (cfg.strength * 0.7))))
            ),
        ),
    )
    sat_threshold = max(
        8,
        min(
            190,
            int(
                0.55 * s_lo
                + 0.45 * max(20, min(160, int(110 - (cfg.strength * 0.5))))
            ),
        ),
    )
    bright = v.point(lambda val: 255 if int(val) >= lum_threshold else 0, mode="L")
    low_sat = s.point(lambda val: 255 if int(val) <= sat_threshold else 0, mode="L")
    bright_text_mask = ImageChops.multiply(bright, low_sat)

    missing_alpha_threshold = max(70, min(240, int(228 - (cfg.strength * 1.25))))
    missing_mask = alpha.point(
        lambda a: 255 if int(a) < missing_alpha_threshold else 0,
        mode="L",
    )
    object_mask = alpha.point(lambda a: 255 if int(a) > 8 else 0, mode="L")
    context_kernel = 5 + 2 * min(3, cfg.strength // 25)
    context_mask = object_mask.filter(ImageFilter.MaxFilter(size=context_kernel))

    candidate = ImageChops.multiply(stroke_mask, missing_mask)
    bright_candidate = ImageChops.multiply(bright_text_mask, missing_mask)
    candidate = ImageChops.lighter(candidate, bright_candidate)
    if cfg.strength < 35:
        candidate = ImageChops.multiply(candidate, context_mask)
    elif cfg.strength < 65:
        soft_context = context_mask.filter(ImageFilter.MaxFilter(size=9))
        candidate = ImageChops.lighter(
            ImageChops.multiply(candidate, soft_context),
            ImageChops.multiply(bright_candidate, missing_mask),
        )
    return candidate


def _roi_box_pixels(
    roi: tuple[float, float, float, float] | None,
    size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    if roi is None:
        return None
    width, height = size
    if width <= 0 or height <= 0:
        return None
    x, y, w, h = roi
    left = int(round(x * width))
    top = int(round(y * height))
    right = int(round((x + w) * width))
    bottom = int(round((y + h) * height))
    left = max(0, min(width - 1, left))
    top = max(0, min(height - 1, top))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _roi_guided_text_candidate_mask(
    rgb: Image.Image,
    alpha: Image.Image,
    cfg: TextPreserveConfig,
) -> Image.Image:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return _heuristic_text_candidate_mask(rgb, alpha, cfg)

    src = np.array(rgb.convert("RGB"))
    if src.size == 0:
        return Image.new("L", rgb.size, 0)
    gray = cv2.cvtColor(src, cv2.COLOR_RGB2GRAY)

    # Color-agnostic enhancement in grayscale space.
    clip_limit = 2.0 + (cfg.strength / 45.0)
    clahe = cv2.createCLAHE(clipLimit=float(clip_limit), tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    upscaled = cv2.resize(
        enhanced,
        None,
        fx=2.0,
        fy=2.0,
        interpolation=cv2.INTER_CUBIC,
    )

    med = float(np.median(upscaled))
    sigma = max(0.10, min(0.45, 0.35 - (cfg.strength / 420.0)))
    canny_low = int(max(18, min(210, (1.0 - sigma) * med)))
    canny_high = int(max(canny_low + 8, min(255, (1.0 + sigma) * med)))
    edges = cv2.Canny(upscaled, canny_low, canny_high)
    adaptive = cv2.adaptiveThreshold(
        upscaled,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        5,
    )

    # K-means color grouping to separate text-like color groups in the ROI.
    k = max(2, min(8, int(cfg.color_groups)))
    lab = cv2.cvtColor(
        cv2.resize(src, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC),
        cv2.COLOR_RGB2LAB,
    ).reshape(-1, 3).astype(np.float32)
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        25,
        0.2,
    )
    _compactness, labels, _centers = cv2.kmeans(
        lab,
        k,
        None,
        criteria,
        2,
        cv2.KMEANS_PP_CENTERS,
    )
    labels_img = labels.reshape(upscaled.shape)
    scores: list[tuple[float, int]] = []
    for idx in range(k):
        cluster = (labels_img == idx).astype("uint8") * 255
        area = int(cv2.countNonZero(cluster))
        if area <= 0:
            continue
        edge_hits = int(cv2.countNonZero(cv2.bitwise_and(cluster, edges)))
        score = edge_hits / max(1.0, area ** 0.5)
        scores.append((score, idx))
    scores.sort(reverse=True)
    keep_count = max(1, min(4, len(scores)))
    color_mask = np.zeros_like(upscaled, dtype="uint8")
    for _score, idx in scores[:keep_count]:
        color_mask[labels_img == idx] = 255

    merged = cv2.bitwise_and(adaptive, color_mask)
    ksz = 3 + 2 * min(2, cfg.strength // 40)
    morph = cv2.getStructuringElement(cv2.MORPH_RECT, (ksz, ksz))
    merged = cv2.morphologyEx(merged, cv2.MORPH_CLOSE, morph, iterations=1)
    merged = cv2.morphologyEx(merged, cv2.MORPH_OPEN, morph, iterations=1)

    # Component filtering to keep text-like structures only.
    num_labels, comp_labels, stats, _centroids = cv2.connectedComponentsWithStats(merged, 8)
    filtered = np.zeros_like(merged, dtype="uint8")
    h, w = merged.shape
    area_total = max(1, h * w)
    min_area = max(16, int(area_total * 0.00006))
    max_area = max(min_area + 1, int(area_total * 0.30))
    for idx in range(1, int(num_labels)):
        x, y, ww, hh, cc_area = stats[idx]
        if cc_area < min_area or cc_area > max_area:
            continue
        ratio = ww / max(1, hh)
        if ratio < 0.06 or ratio > 28.0:
            continue
        fill_ratio = cc_area / max(1, ww * hh)
        if fill_ratio < 0.05 or fill_ratio > 0.97:
            continue
        filtered[comp_labels == idx] = 255

    # Downscale back to ROI size.
    core = cv2.resize(filtered, (rgb.width, rgb.height), interpolation=cv2.INTER_AREA)
    core = (core > 0).astype("uint8") * 255

    # Keep only where source has alpha support.
    alpha_arr = np.array(alpha.convert("L"))
    if alpha_arr.shape == core.shape:
        core[alpha_arr <= 0] = 0

    return Image.fromarray(core.astype("uint8"), mode="L")


def _hist_percentile(gray: Image.Image, percentile: float) -> int:
    value = max(0.0, min(100.0, float(percentile)))
    hist = gray.histogram()
    if not hist:
        return 0
    total = int(sum(hist))
    if total <= 0:
        return 0
    target = int(round((value / 100.0) * (total - 1)))
    acc = 0
    for idx, count in enumerate(hist):
        acc += int(count)
        if acc > target:
            return idx
    return len(hist) - 1


def _apply_text_candidate_effects(
    core_candidate: Image.Image,
    rgb: Image.Image,
    cfg: TextPreserveConfig,
) -> Image.Image:
    result = core_candidate.convert("L")
    gray = rgb.convert("L")
    seed_core = result
    core = _expand_seed_color_regions(seed_core, rgb, cfg)
    core = _augment_with_horizontal_bars(core, rgb, cfg)
    core = _suppress_text_flakes(core, seed_core, cfg)
    result = core

    if core.getbbox() is None:
        return result

    if cfg.include_outline:
        outline_radius = max(1, int(1 + (cfg.strength / 45.0)))
        outline_kernel = (outline_radius * 2) + 1
        dilated = core.filter(ImageFilter.MaxFilter(size=outline_kernel))
        outline_band = ImageChops.subtract(dilated, core)
        outline_band = outline_band.point(
            lambda value: int(min(255, round(int(value) * 0.70))),
            mode="L",
        )
        result = ImageChops.lighter(result, outline_band)

    if cfg.include_shadow:
        shadow_radius = max(1, int(1 + cfg.strength / 55.0))
        shadow_kernel = (shadow_radius * 2) + 1
        shadow_band = ImageChops.subtract(
            core.filter(ImageFilter.MaxFilter(size=shadow_kernel)),
            core,
        )
        dark_threshold = _hist_percentile(gray, 35.0)
        dark_gate = gray.point(
            lambda value: 255 if int(value) <= dark_threshold else 0,
            mode="L",
        )
        shadow = ImageChops.multiply(shadow_band, dark_gate).point(
            lambda value: int(min(255, round(int(value) * 0.50))),
            mode="L",
        )
        result = ImageChops.lighter(result, shadow)

    if cfg.glow_mode != "disabled" and cfg.glow_radius > 0 and cfg.glow_strength > 0:
        glow_radius = int(cfg.glow_radius)
        glow_kernel = (glow_radius * 2) + 1
        glow_band = ImageChops.subtract(
            core.filter(ImageFilter.MaxFilter(size=glow_kernel)),
            core,
        )
        glow_blur = glow_band.filter(
            ImageFilter.GaussianBlur(radius=max(0.6, glow_radius * 0.6))
        )
        bright_threshold = _hist_percentile(gray, 70.0)
        dark_threshold = _hist_percentile(gray, 30.0)
        glow_gate = Image.new("L", gray.size, 0)
        if cfg.glow_mode in {"bright", "both"}:
            bright_gate = gray.point(
                lambda value: 255 if int(value) >= bright_threshold else 0,
                mode="L",
            )
            glow_gate = ImageChops.lighter(glow_gate, bright_gate)
        if cfg.glow_mode in {"dark", "both"}:
            dark_gate = gray.point(
                lambda value: 255 if int(value) <= dark_threshold else 0,
                mode="L",
            )
            glow_gate = ImageChops.lighter(glow_gate, dark_gate)
        glow_alpha = ImageChops.multiply(glow_blur, glow_gate).point(
            lambda value: int(
                min(255, round(int(value) * (cfg.glow_strength / 100.0) * 0.65))
            ),
            mode="L",
        )
        # Keep glow soft: alpha-keyed only, never opaque replacement.
        result = ImageChops.lighter(result, glow_alpha)

    return result


def _augment_with_horizontal_bars(
    core_candidate: Image.Image,
    rgb: Image.Image,
    cfg: TextPreserveConfig,
) -> Image.Image:
    """Add color-agnostic horizontal bar companions near extracted text.

    Some game logos include decorative bars aligned with text baselines.
    These bars can be weak in OCR/color clustering but strong in geometry.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return core_candidate

    core = core_candidate.convert("L")
    core_arr = np.array(core, dtype=np.uint8)
    if core_arr.size == 0:
        return core

    # Need at least some text seed before attaching companion bars.
    seed_rows = np.where(core_arr.sum(axis=1) > 0)[0]
    if seed_rows.size == 0:
        return core

    gray = np.array(rgb.convert("L"), dtype=np.uint8)
    if gray.shape != core_arr.shape:
        return core

    h, w = gray.shape
    if h < 12 or w < 24:
        return core

    gray_up = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    core_up = cv2.resize(core_arr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)

    low = max(20, int(90 - cfg.strength * 0.4))
    high = max(60, int(210 - cfg.strength * 0.85))
    edges = cv2.Canny(gray_up, low, high)
    if not np.any(edges):
        return core

    # Bridge line fragments while suppressing vertical clutter.
    dil = cv2.dilate(
        edges,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    close_w = max(14, int(gray_up.shape[1] * 0.10))
    close_h = max(1, int(gray_up.shape[0] * 0.018))
    bars = cv2.morphologyEx(
        dil,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (close_w, close_h)),
        iterations=1,
    )
    open_w = max(8, int(gray_up.shape[1] * 0.045))
    bars = cv2.morphologyEx(
        bars,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (open_w, 1)),
        iterations=1,
    )

    if not np.any(bars):
        return core

    # Keep elongated components around the text vertical band.
    text_rows_up = np.where(core_up.sum(axis=1) > 0)[0]
    if text_rows_up.size == 0:
        return core
    text_top = int(text_rows_up.min())
    text_bottom = int(text_rows_up.max())
    band_pad = max(6, int(gray_up.shape[0] * 0.20))
    band_top = max(0, text_top - band_pad)
    band_bottom = min(gray_up.shape[0] - 1, text_bottom + band_pad)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(bars, 8)
    kept = np.zeros_like(bars, dtype=np.uint8)
    area_total = max(1, gray_up.shape[0] * gray_up.shape[1])
    min_area = max(20, int(area_total * 0.00008))
    max_area = max(min_area + 1, int(area_total * 0.22))
    min_width = max(14, int(gray_up.shape[1] * 0.08))
    max_height = max(4, int(gray_up.shape[0] * 0.24))
    for idx in range(1, int(num_labels)):
        x, y, ww, hh, area = stats[idx]
        if area < min_area or area > max_area:
            continue
        if ww < min_width or hh > max_height:
            continue
        ratio = ww / max(1, hh)
        if ratio < 4.0:
            continue
        fill = area / max(1, ww * hh)
        if fill < 0.10:
            continue
        cy = y + (hh // 2)
        if cy < band_top or cy > band_bottom:
            continue
        kept[labels == idx] = 255

    if not np.any(kept):
        return core

    kept_down = cv2.resize(
        kept,
        (core_arr.shape[1], core_arr.shape[0]),
        interpolation=cv2.INTER_AREA,
    )
    kept_down = (kept_down > 0).astype(np.uint8) * 255
    merged = np.maximum(core_arr, kept_down).astype(np.uint8)
    return Image.fromarray(merged, mode="L")


def _expand_seed_color_regions(
    core_candidate: Image.Image,
    rgb: Image.Image,
    cfg: TextPreserveConfig,
) -> Image.Image:
    """Grow text seed into full glyph regions using local color clusters."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return core_candidate

    seed = np.array(core_candidate.convert("L"), dtype=np.uint8)
    seed_bin = (seed > 0).astype(np.uint8) * 255
    seed_pixels = int(np.count_nonzero(seed_bin))
    if seed_pixels <= 0:
        return core_candidate

    src = np.array(rgb.convert("RGB"), dtype=np.uint8)
    if src.shape[:2] != seed.shape:
        return core_candidate
    h, w = seed.shape
    if h < 8 or w < 8:
        return core_candidate

    lab = cv2.cvtColor(src, cv2.COLOR_RGB2LAB)
    data = lab.reshape(-1, 3).astype(np.float32)
    k = max(2, min(8, int(cfg.color_groups)))
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        20,
        0.2,
    )
    _compactness, labels, centers = cv2.kmeans(
        data,
        k,
        None,
        criteria,
        2,
        cv2.KMEANS_PP_CENTERS,
    )
    labels_img = labels.reshape(h, w)
    center_arr = centers.astype(np.float32)

    seed_rows = np.where(seed_bin.sum(axis=1) > 0)[0]
    band_top = int(seed_rows.min()) if seed_rows.size else 0
    band_bottom = int(seed_rows.max()) if seed_rows.size else (h - 1)
    band_pad = max(6, int(h * 0.16))
    band_top = max(0, band_top - band_pad)
    band_bottom = min(h - 1, band_bottom + band_pad)
    text_band = np.zeros((h, w), dtype=bool)
    text_band[band_top : band_bottom + 1, :] = True

    keep_clusters: list[int] = []
    min_seed_hits = max(6, int(seed_pixels * 0.01))
    for idx in range(k):
        cluster = labels_img == idx
        area = int(np.count_nonzero(cluster))
        if area <= 0:
            continue
        hits = int(np.count_nonzero(cluster & (seed_bin > 0)))
        hit_ratio = hits / float(area)
        if hits >= min_seed_hits or hit_ratio >= 0.025:
            keep_clusters.append(idx)

    if cfg.seed_colors:
        seed_rgb = np.array(cfg.seed_colors, dtype=np.uint8).reshape((-1, 1, 3))
        seed_lab = cv2.cvtColor(seed_rgb, cv2.COLOR_RGB2LAB).reshape((-1, 3)).astype(np.float32)
        tolerance = float(max(4, min(96, int(cfg.seed_tolerance))))

        for idx in range(k):
            if idx in keep_clusters:
                continue
            nearest = min(
                float(np.linalg.norm(center_arr[idx] - ref))
                for ref in seed_lab
            )
            if nearest <= tolerance:
                keep_clusters.append(idx)

    # Gradient-consistent expansion: keep neighboring color clusters near seed
    # clusters when they appear in the text band.
    if keep_clusters:
        dist_threshold = 24.0 + float(cfg.strength) * 0.24
        for idx in range(k):
            if idx in keep_clusters:
                continue
            cluster = labels_img == idx
            band_pixels = int(np.count_nonzero(cluster & text_band))
            if band_pixels < max(10, int(w * 0.01)):
                continue
            nearest = min(
                float(np.linalg.norm(center_arr[idx] - center_arr[seed_idx]))
                for seed_idx in keep_clusters
            )
            if nearest <= dist_threshold:
                keep_clusters.append(idx)

    if not keep_clusters:
        return core_candidate

    cluster_mask = np.isin(labels_img, keep_clusters).astype(np.uint8) * 255
    if cfg.seed_colors:
        seed_rgb = np.array(cfg.seed_colors, dtype=np.uint8).reshape((-1, 1, 3))
        seed_lab = cv2.cvtColor(seed_rgb, cv2.COLOR_RGB2LAB).reshape((-1, 3)).astype(np.float32)
        tolerance = float(max(4, min(96, int(cfg.seed_tolerance))))
        dists = np.stack(
            [np.linalg.norm(lab.astype(np.float32) - ref, axis=2) for ref in seed_lab],
            axis=2,
        )
        color_match = (np.min(dists, axis=2) <= tolerance).astype(np.uint8) * 255
        cluster_mask = np.maximum(cluster_mask, color_match).astype(np.uint8)
    prox_x = max(4, int(w * (0.025 + (cfg.strength / 900.0))))
    prox_y = max(2, int(h * (0.010 + (cfg.strength / 1800.0))))
    prox_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (prox_x * 2 + 1, prox_y * 2 + 1)
    )
    seed_proximity = cv2.dilate(seed_bin, prox_kernel, iterations=1)
    grown = cv2.bitwise_and(cluster_mask, seed_proximity)
    ksz = 3 + 2 * min(2, cfg.strength // 45)
    grown = cv2.morphologyEx(
        grown,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (ksz, ksz)),
        iterations=1,
    )
    # Keep only components with strong seed support or bar-like geometry.
    band_pad = max(6, int(h * 0.18))
    band_top = max(0, band_top - band_pad)
    band_bottom = min(h - 1, band_bottom + band_pad)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(grown, 8)
    filtered = np.zeros_like(grown, dtype=np.uint8)
    area_total = max(1, h * w)
    max_component_area = max(24, int(area_total * 0.35))
    min_component_area = max(8, int(area_total * 0.00004))
    min_ratio = 0.010
    for idx in range(1, int(num_labels)):
        x, y, ww, hh, area = stats[idx]
        if area < min_component_area or area > max_component_area:
            continue
        comp = labels == idx
        seed_hits = int(np.count_nonzero(comp & (seed_bin > 0)))
        seed_ratio = seed_hits / float(max(1, area))
        if seed_hits >= max(6, int(seed_pixels * 0.006)) or seed_ratio >= min_ratio:
            filtered[comp] = 255
            continue
        ratio = ww / max(1, hh)
        fill = area / max(1, ww * hh)
        cy = y + (hh // 2)
        if ww >= max(14, int(w * 0.08)) and ratio >= 4.0 and fill >= 0.10:
            if band_top <= cy <= band_bottom:
                filtered[comp] = 255

    merged = np.maximum(seed_bin, filtered).astype(np.uint8)
    return Image.fromarray(merged, mode="L")


def _suppress_text_flakes(
    candidate: Image.Image,
    seed_core: Image.Image,
    cfg: TextPreserveConfig,
) -> Image.Image:
    """Remove isolated background flakes while keeping seed-connected text/bars."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return candidate

    cand = np.array(candidate.convert("L"), dtype=np.uint8)
    seed = np.array(seed_core.convert("L"), dtype=np.uint8)
    cand_bin = (cand > 0).astype(np.uint8) * 255
    seed_bin = (seed > 0).astype(np.uint8) * 255
    if not np.any(cand_bin) or not np.any(seed_bin):
        return candidate
    if int(np.count_nonzero(seed_bin)) <= 48 or int(np.count_nonzero(cand_bin)) <= 96:
        return candidate

    h, w = cand_bin.shape
    prox_radius = max(4, int(min(h, w) * (0.05 + (cfg.strength / 700.0))))
    prox_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (prox_radius * 2 + 1, prox_radius * 2 + 1)
    )
    seed_proximity = cv2.dilate(seed_bin, prox_kernel, iterations=1)

    seed_rows = np.where(seed_bin.sum(axis=1) > 0)[0]
    band_top = int(seed_rows.min()) if seed_rows.size else 0
    band_bottom = int(seed_rows.max()) if seed_rows.size else (h - 1)
    band_pad = max(6, int(h * 0.20))
    band_top = max(0, band_top - band_pad)
    band_bottom = min(h - 1, band_bottom + band_pad)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(cand_bin, 8)
    kept = np.zeros_like(cand_bin, dtype=np.uint8)
    area_total = max(1, h * w)
    min_area = max(8, int(area_total * 0.00005))
    max_area = max(min_area + 1, int(area_total * 0.35))
    for idx in range(1, int(num_labels)):
        x, y, ww, hh, area = stats[idx]
        component_mask = labels == idx
        touches_seed = bool(np.any(component_mask & (seed_proximity > 0)))
        if touches_seed:
            seed_hits_total = int(np.count_nonzero(component_mask & (seed_bin > 0)))
            seed_ratio_total = seed_hits_total / float(max(1, area))
            if seed_hits_total >= 8 or seed_ratio_total >= 0.02:
                near_seed = component_mask & (seed_proximity > 0)
                if np.any(near_seed):
                    kept[near_seed] = 255
                continue
        if area < min_area or area > max_area:
            continue
        if touches_seed:
            near_seed = component_mask & (seed_proximity > 0)
            if np.any(near_seed):
                kept[near_seed] = 255
            continue
        # Preserve detached companion bars if they are elongated and near text rows.
        ratio = ww / max(1, hh)
        fill = area / max(1, ww * hh)
        cy = y + (hh // 2)
        if ww >= max(14, int(w * 0.08)) and ratio >= 4.0 and fill >= 0.10:
            if band_top <= cy <= band_bottom:
                kept[component_mask] = 255

    if np.any(kept):
        softened = cv2.dilate(
            kept,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=1,
        )
        kept = cv2.bitwise_and(softened, cand_bin)
    return Image.fromarray(kept, mode="L")


def _mask_nonzero_ratio(mask: Image.Image) -> float:
    hist = mask.convert("L").histogram()
    total = int(sum(hist))
    if total <= 0:
        return 0.0
    nonzero = total - int(hist[0] if hist else 0)
    return nonzero / float(total)


def _map_manual_points_to_work_pixels(
    points: tuple[tuple[float, float], ...],
    image_size: tuple[int, int],
    roi_box: tuple[int, int, int, int] | None,
) -> list[tuple[int, int]]:
    width = max(1, int(image_size[0]))
    height = max(1, int(image_size[1]))
    mapped: list[tuple[int, int]] = []
    for x_norm, y_norm in points:
        x_px = int(round(max(0.0, min(1.0, float(x_norm))) * (width - 1)))
        y_px = int(round(max(0.0, min(1.0, float(y_norm))) * (height - 1)))
        if roi_box is None:
            point = (x_px, y_px)
        else:
            left, top, right, bottom = roi_box
            if x_px < left or x_px >= right or y_px < top or y_px >= bottom:
                continue
            point = (x_px - left, y_px - top)
        if point not in mapped:
            mapped.append(point)
    return mapped


def _manual_seed_region_mask(
    rgb: Image.Image,
    seed_points: list[tuple[int, int]],
    cfg: TextPreserveConfig,
) -> Image.Image:
    try:
        import cv2
        import numpy as np
    except ImportError:
        fallback = Image.new("L", rgb.size, 0)
        draw = ImageDraw.Draw(fallback)
        radius = max(4, 2 + cfg.feather)
        for x_px, y_px in seed_points:
            draw.ellipse(
                (x_px - radius, y_px - radius, x_px + radius, y_px + radius),
                fill=255,
            )
        return fallback

    arr = np.array(rgb.convert("RGB"), dtype=np.uint8)
    if arr.size == 0:
        return Image.new("L", rgb.size, 0)
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    low = max(20, int(95 - cfg.strength * 0.35))
    high = max(60, int(220 - cfg.strength * 0.75))
    edges = cv2.Canny(gray, low, high)

    tol = int(max(8, min(96, cfg.seed_tolerance + int(cfg.strength * 0.18))))
    max_radius = int(max(18, round(min(h, w) * (0.09 + (cfg.strength / 1200.0)))))

    combined = np.zeros((h, w), dtype=np.uint8)
    for x_px, y_px in seed_points:
        if not (0 <= x_px < w and 0 <= y_px < h):
            continue
        seed_src = arr.copy()
        ff_mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
        ff_mask[1:-1, 1:-1][edges > 0] = 1
        ff_mask[y_px + 1, x_px + 1] = 0
        flags = 4 | cv2.FLOODFILL_FIXED_RANGE | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
        try:
            cv2.floodFill(
                seed_src,
                ff_mask,
                (x_px, y_px),
                (0, 0, 0),
                (tol, tol, tol),
                (tol, tol, tol),
                flags,
            )
        except cv2.error:
            continue
        region = (ff_mask[1:-1, 1:-1] == 255).astype(np.uint8) * 255
        if max_radius > 0:
            y_grid, x_grid = np.ogrid[:h, :w]
            rad_mask = ((x_grid - x_px) ** 2 + (y_grid - y_px) ** 2) <= (max_radius**2)
            region = np.where(rad_mask, region, 0).astype(np.uint8)
        combined = np.maximum(combined, region).astype(np.uint8)
    if np.any(combined):
        combined = cv2.morphologyEx(
            combined,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=1,
        )
    return Image.fromarray(combined, mode="L")


def _build_text_overlay_alpha_mask(
    source_master: Image.Image,
    cutout_master: Image.Image,
    config: TextPreserveConfig | dict[str, object] | None,
) -> Image.Image | None:
    cfg = normalize_text_preserve_config(config)
    has_manual = bool(cfg.manual_add_seeds) or bool(cfg.manual_remove_seeds)
    if (not cfg.enabled and not has_manual) or (cfg.method == "none" and not has_manual):
        return None

    original = source_master.convert("RGBA")
    cutout = cutout_master.convert("RGBA")
    alpha = cutout.getchannel("A")
    source_alpha = original.getchannel("A")
    rgb = original.convert("RGB")

    roi_box = _roi_box_pixels(cfg.roi, rgb.size)
    roi_hard_mask: Image.Image | None = None
    if roi_box is None:
        work_rgb = rgb
        work_alpha = alpha
    else:
        work_rgb = rgb.crop(roi_box)
        work_alpha = alpha.crop(roi_box)
        roi_hard_mask = Image.new("L", rgb.size, 0)
        roi_hard_mask.paste(255, roi_box)

    if cfg.method == "none":
        core_candidate = Image.new("L", work_rgb.size, 0)
    else:
        if cfg.method == "heuristic":
            core_candidate = _heuristic_text_candidate_mask(work_rgb, work_alpha, cfg)
        elif cfg.method == "roi_guided":
            # ROI-guided should not underperform heuristic: blend both and
            # fallback to heuristic when ROI candidate is too sparse.
            heuristic_candidate = _heuristic_text_candidate_mask(work_rgb, work_alpha, cfg)
            roi_candidate = _roi_guided_text_candidate_mask(work_rgb, work_alpha, cfg)
            if _mask_nonzero_ratio(roi_candidate) <= 0.0005:
                core_candidate = heuristic_candidate
            else:
                core_candidate = ImageChops.lighter(heuristic_candidate, roi_candidate)
        else:
            # OCR/DB detections should preserve text even when cutout kept parts of it.
            core_candidate = _detected_text_mask(work_rgb, cfg.method, cfg.strength)

    add_points = _map_manual_points_to_work_pixels(cfg.manual_add_seeds, rgb.size, roi_box)
    remove_points = _map_manual_points_to_work_pixels(cfg.manual_remove_seeds, rgb.size, roi_box)
    add_mask: Image.Image | None = None
    remove_mask: Image.Image | None = None
    manual_core = core_candidate
    if add_points:
        add_mask = _manual_seed_region_mask(work_rgb, add_points, cfg)
        manual_core = ImageChops.lighter(manual_core, add_mask)
    if remove_points:
        remove_mask = _manual_seed_region_mask(work_rgb, remove_points, cfg)
        manual_core = ImageChops.subtract(manual_core, remove_mask)
    candidate = _apply_text_candidate_effects(manual_core, work_rgb, cfg)
    if remove_mask is not None:
        remove_final = remove_mask
        if cfg.include_outline or cfg.include_shadow or cfg.glow_mode != "disabled":
            remove_final = remove_final.filter(ImageFilter.MaxFilter(size=5))
        candidate = ImageChops.subtract(candidate, remove_final)
    grow_kernel = 3 + 2 * min(2, cfg.strength // 50)
    candidate = candidate.filter(ImageFilter.MaxFilter(size=grow_kernel))
    if cfg.feather > 0:
        candidate = candidate.filter(ImageFilter.GaussianBlur(radius=float(cfg.feather)))
    candidate_floor = max(6, min(42, int(24 - (cfg.strength * 0.12))))
    candidate = candidate.point(
        lambda value: 0 if int(value) < candidate_floor else int(value),
        mode="L",
    )

    if roi_box is not None:
        full_candidate = Image.new("L", rgb.size, 0)
        full_candidate.paste(candidate, (roi_box[0], roi_box[1]))
        candidate = full_candidate

    overlay_alpha = ImageChops.multiply(candidate, source_alpha)
    if roi_hard_mask is not None:
        # Strict ROI mode: never keep any pixel outside user ROI.
        overlay_alpha = ImageChops.multiply(overlay_alpha, roi_hard_mask)
    alpha_extrema = overlay_alpha.getextrema()
    if alpha_extrema is None or alpha_extrema[1] <= 0:
        return None
    return overlay_alpha


def build_text_extraction_alpha_mask(
    source_master: Image.Image,
    cutout_master: Image.Image,
    config: TextPreserveConfig | dict[str, object] | None,
) -> Image.Image | None:
    return _build_text_overlay_alpha_mask(source_master, cutout_master, config)


def build_text_extraction_overlay(
    source_master: Image.Image,
    cutout_master: Image.Image,
    config: TextPreserveConfig | dict[str, object] | None,
) -> Image.Image | None:
    overlay_alpha = _build_text_overlay_alpha_mask(
        source_master,
        cutout_master,
        config,
    )
    if overlay_alpha is None:
        return None
    original = source_master.convert("RGBA")
    text_overlay = original.copy()
    text_overlay.putalpha(overlay_alpha)
    return text_overlay


def apply_text_preserve_to_cutout(
    source_master: Image.Image,
    cutout_master: Image.Image,
    config: TextPreserveConfig | dict[str, object] | None,
) -> Image.Image:
    cutout = cutout_master.convert("RGBA")
    text_overlay = build_text_extraction_overlay(source_master, cutout, config)
    if text_overlay is None:
        return cutout
    # Explicit ordering: background-removal layer first, text extraction overlay on top.
    return Image.alpha_composite(cutout, text_overlay)


def _shader_rgb(config: BorderShaderConfig) -> tuple[int, int, int]:
    hue = config.hue / 360.0
    sat = config.saturation / 100.0
    tone = config.tone / 100.0
    if config.mode == "hsl":
        red, green, blue = colorsys.hls_to_rgb(hue, tone, sat)
    else:
        red, green, blue = colorsys.hsv_to_rgb(hue, sat, tone)
    return (
        max(0, min(255, int(round(red * 255.0)))),
        max(0, min(255, int(round(green * 255.0)))),
        max(0, min(255, int(round(blue * 255.0)))),
    )


def _analysis_sidecar_path(template_path: Path) -> Path:
    return template_path.with_suffix(".analysis.json")


def _shape_from_mask(mask: Image.Image | None) -> str | None:
    if mask is None:
        return None
    bbox = mask.getbbox()
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    if abs(width - height) > max(2, int(max(width, height) * 0.15)):
        return "square"
    values = mask.tobytes()
    area = sum(1 for value in values if value > 20)
    fill_ratio = area / float(width * height)
    # Circle-like interiors tend to be around pi/4 of the bounding square.
    if 0.68 <= fill_ratio <= 0.86:
        return "round"
    return "square"


def _analyze_template_alpha(
    overlay: Image.Image,
    alpha_threshold: int = 8,
) -> tuple[Image.Image | None, dict[str, object]]:
    alpha = overlay.getchannel("A")
    width, height = alpha.size
    total = width * height
    if total <= 0:
        return None, {"interior_pixels": 0, "outside_pixels": 0, "transparent_pixels": 0}

    alpha_bytes = alpha.tobytes()
    transparent = [value <= alpha_threshold for value in alpha_bytes]
    outside = [False] * total
    queue: deque[int] = deque()

    def _enqueue(index: int) -> None:
        if 0 <= index < total and transparent[index] and not outside[index]:
            outside[index] = True
            queue.append(index)

    for x in range(width):
        _enqueue(x)
        _enqueue((height - 1) * width + x)
    for y in range(height):
        _enqueue(y * width)
        _enqueue(y * width + (width - 1))

    while queue:
        idx = queue.popleft()
        x = idx % width
        y = idx // width
        if x > 0:
            _enqueue(idx - 1)
        if x + 1 < width:
            _enqueue(idx + 1)
        if y > 0:
            _enqueue(idx - width)
        if y + 1 < height:
            _enqueue(idx + width)

    interior_bytes = bytearray(total)
    interior_pixels = 0
    transparent_pixels = 0
    outside_pixels = 0
    min_x, min_y = width, height
    max_x, max_y = -1, -1
    for idx in range(total):
        if not transparent[idx]:
            continue
        transparent_pixels += 1
        if outside[idx]:
            outside_pixels += 1
            continue
        interior_bytes[idx] = 255
        interior_pixels += 1
        x = idx % width
        y = idx // width
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y

    if interior_pixels <= 0:
        return None, {
            "interior_pixels": 0,
            "outside_pixels": outside_pixels,
            "transparent_pixels": transparent_pixels,
        }

    interior_mask = Image.frombytes("L", (width, height), bytes(interior_bytes))
    bbox = [int(min_x), int(min_y), int(max_x + 1), int(max_y + 1)]
    stats: dict[str, object] = {
        "interior_pixels": interior_pixels,
        "outside_pixels": outside_pixels,
        "transparent_pixels": transparent_pixels,
        "interior_bbox": bbox,
    }
    return interior_mask, stats


def _write_template_analysis_metadata(
    template_path: Path,
    *,
    template_mtime_ns: int,
    shape: str | None,
    stats: dict[str, object],
) -> None:
    payload = {
        "template_file": template_path.name,
        "template_mtime_ns": int(template_mtime_ns),
        "shape": shape or "",
        "alpha_threshold": 8,
        **stats,
    }
    sidecar = _analysis_sidecar_path(template_path)
    try:
        sidecar.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


@lru_cache(maxsize=64)
def _load_template_analysis(path_text: str, template_mtime_ns: int) -> TemplateAnalysis | None:
    if not path_text:
        return None
    template_path = Path(path_text)
    if not template_path.exists():
        return None
    try:
        image = Image.open(template_path)
        image.load()
        overlay = image.convert("RGBA")
    except OSError:
        return None
    interior_mask, stats = _analyze_template_alpha(overlay, alpha_threshold=8)
    shape_meta, _label_meta = _parse_template_metadata(template_path)
    shape = shape_meta or _shape_from_mask(interior_mask) or _shape_from_name(template_path.stem)
    _write_template_analysis_metadata(
        template_path,
        template_mtime_ns=template_mtime_ns,
        shape=shape,
        stats=stats,
    )
    return TemplateAnalysis(
        overlay=overlay,
        interior_mask=interior_mask,
        shape=shape,
    )


def _get_template_analysis(template: IconTemplate) -> TemplateAnalysis | None:
    if template.path is None:
        return None
    try:
        mtime_ns = int(template.path.stat().st_mtime_ns)
    except OSError:
        return None
    return _load_template_analysis(str(template.path), mtime_ns)


def _inner_shape_mask(shape: str, size: int) -> Image.Image:
    inset = max(2, int(size * 0.035))
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    if shape == "round":
        draw.ellipse(
            (inset, inset, size - inset - 1, size - inset - 1),
            fill=255,
        )
    else:
        radius = max(6, int(size * 0.10))
        draw.rounded_rectangle(
            (inset, inset, size - inset - 1, size - inset - 1),
            radius=radius,
            fill=255,
        )
    return mask


def _fit_to_square(image: Image.Image, size: int) -> Image.Image:
    fitted = ImageOps.contain(image, (size, size), method=Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(
        fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2)
    )
    return out


def _apply_template_overlay(
    image: Image.Image,
    size: int,
    template: IconTemplate,
    border_shader: BorderShaderConfig | dict[str, object] | None = None,
) -> Image.Image:
    if template.template_id == "none":
        return image
    analysis = _get_template_analysis(template)
    if analysis is not None:
        overlay = analysis.overlay.resize((size, size), Image.Resampling.LANCZOS)
    else:
        # Non-file-backed generic overlays are intentionally not used.
        return image
    overlay = _apply_border_shader(overlay, border_shader)
    return Image.alpha_composite(image, overlay)


def _template_interior_mask(template: IconTemplate, size: int) -> Image.Image | None:
    analysis = _get_template_analysis(template)
    if analysis is not None and analysis.interior_mask is not None:
        return analysis.interior_mask.resize((size, size), Image.Resampling.LANCZOS)
    # Non-file-backed generic masks are intentionally not used.
    return None


def build_template_interior_mask_png(
    icon_style: str | None,
    size: int = 256,
    *,
    circular_ring: bool | None = None,
) -> bytes | None:
    template = resolve_icon_template(icon_style, circular_ring)
    if template.template_id == "none":
        return None
    mask = _template_interior_mask(template, size)
    if mask is None:
        return None
    out = BytesIO()
    mask.save(out, format="PNG")
    return out.getvalue()


def _apply_border_shader(
    overlay: Image.Image,
    border_shader: BorderShaderConfig | dict[str, object] | None,
) -> Image.Image:
    shader = normalize_border_shader_config(border_shader)
    if not shader.enabled or shader.intensity <= 0:
        return overlay
    if overlay.mode != "RGBA":
        overlay = overlay.convert("RGBA")
    tint_rgb = _shader_rgb(shader)
    tint_layer = Image.new("RGBA", overlay.size, (*tint_rgb, 255))
    blend_alpha = shader.intensity / 100.0
    blended = Image.blend(overlay, tint_layer, blend_alpha)
    blended.putalpha(overlay.getchannel("A"))
    return blended


def _build_master(image_bytes: bytes) -> Image.Image:
    image = Image.open(BytesIO(image_bytes))
    image.load()
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA")
    return _crop_square(image)


def _build_composited_icon(
    master: Image.Image,
    size: int,
    template: IconTemplate,
    foreground: Image.Image | None = None,
    border_shader: BorderShaderConfig | dict[str, object] | None = None,
) -> Image.Image:
    if template.template_id == "none":
        base = _fit_to_square(master, size)
    else:
        source = _fit_to_square(master, size)
        base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        interior_mask = _template_interior_mask(template, size)
        if interior_mask is not None:
            # Keep outside area transparent; black-fill only within template interior.
            black_fill = Image.new("RGBA", (size, size), (0, 0, 0, 255))
            base.paste(black_fill, (0, 0), interior_mask)
            src_alpha = source.getchannel("A")
            combined_mask = ImageChops.multiply(src_alpha, interior_mask)
            base.paste(source, (0, 0), combined_mask)
        else:
            base.alpha_composite(source)
    base = _apply_template_overlay(base, size, template, border_shader=border_shader)
    if foreground is not None:
        base = Image.alpha_composite(base, _fit_to_square(foreground, size))
    return base


def _escape_foreground_image(
    source_image_bytes: bytes,
    bg_removal_engine: str | None,
    bg_removal_params: dict[str, object] | None = None,
    text_preserve_config: TextPreserveConfig | dict[str, object] | None = None,
) -> Image.Image | None:
    engine = normalize_background_removal_engine(bg_removal_engine)
    source_master = _build_master(source_image_bytes)
    text_cfg = normalize_text_preserve_config(text_preserve_config)

    if engine == "none":
        if not text_cfg.enabled:
            return None
        foreground = Image.new("RGBA", source_master.size, (0, 0, 0, 0))
    else:
        cutout = remove_background_bytes(
            source_image_bytes,
            engine=engine,
            params=normalize_background_removal_params(bg_removal_params),
        )
        foreground = _build_master(cutout)

    foreground = apply_text_preserve_to_cutout(
        source_master,
        foreground,
        text_cfg,
    )
    alpha_extrema = foreground.getchannel("A").getextrema()
    if alpha_extrema is None or alpha_extrema[1] <= 0:
        return None
    # If cutout returned fully opaque output, treat it as failed background
    # removal and skip the overlay layer to avoid covering the framed icon.
    if engine != "none" and alpha_extrema[0] >= 255:
        return None
    return foreground


def _effective_overlay_modes(
    template: IconTemplate,
    bg_removal_engine: str | None,
    text_preserve_config: TextPreserveConfig | dict[str, object] | None,
) -> tuple[str, TextPreserveConfig]:
    # Global rule: when template is Disabled, cutout/text layers are disabled.
    if template.template_id == "none":
        return "none", TextPreserveConfig(enabled=False, strength=45, feather=1, method="none")
    return (
        normalize_background_removal_engine(bg_removal_engine),
        normalize_text_preserve_config(text_preserve_config),
    )


def build_template_overlay_preview(
    icon_style: str | None,
    size: int = 256,
    border_shader: BorderShaderConfig | dict[str, object] | None = None,
) -> bytes:
    template = resolve_icon_template(icon_style, circular_ring=False)
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rendered = _apply_template_overlay(base, size, template, border_shader=border_shader)
    out = BytesIO()
    rendered.save(out, format="PNG")
    return out.getvalue()


def build_multi_size_ico(
    image_bytes: bytes,
    circular_ring: bool | None = False,
    icon_style: str | None = None,
    bg_removal_engine: str | None = None,
    bg_removal_params: dict[str, object] | None = None,
    text_preserve_config: TextPreserveConfig | dict[str, object] | None = None,
    border_shader: BorderShaderConfig | dict[str, object] | None = None,
) -> bytes:
    template = resolve_icon_template(icon_style, circular_ring)
    effective_engine, effective_text_cfg = _effective_overlay_modes(
        template,
        bg_removal_engine,
        text_preserve_config,
    )
    master = _build_master(image_bytes)
    foreground = _escape_foreground_image(
        image_bytes,
        effective_engine,
        bg_removal_params=bg_removal_params,
        text_preserve_config=effective_text_cfg,
    )
    base = _build_composited_icon(
        master,
        512,
        template,
        foreground=foreground,
        border_shader=border_shader,
    )
    base = _apply_size_profile(base, 256)

    out = BytesIO()
    base.save(out, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return out.getvalue()


def build_preview_png(
    image_bytes: bytes,
    size: int = 96,
    circular_ring: bool | None = False,
    icon_style: str | None = None,
    bg_removal_engine: str | None = None,
    bg_removal_params: dict[str, object] | None = None,
    text_preserve_config: TextPreserveConfig | dict[str, object] | None = None,
    border_shader: BorderShaderConfig | dict[str, object] | None = None,
) -> bytes:
    template = resolve_icon_template(icon_style, circular_ring)
    effective_engine, effective_text_cfg = _effective_overlay_modes(
        template,
        bg_removal_engine,
        text_preserve_config,
    )
    master = _build_master(image_bytes)
    foreground = _escape_foreground_image(
        image_bytes,
        effective_engine,
        bg_removal_params=bg_removal_params,
        text_preserve_config=effective_text_cfg,
    )
    image = _build_composited_icon(
        master,
        size,
        template,
        foreground=foreground,
        border_shader=border_shader,
    )
    image = _apply_size_profile(image, size)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def default_bg_removal_params() -> dict[str, object]:
    return dict(DEFAULT_BG_REMOVAL_PARAMS)
