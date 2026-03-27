from __future__ import annotations

from collections import deque
import colorsys
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import json
import re
from pathlib import Path
import threading

from PIL import Image, ImageChops, ImageDraw, ImageOps


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


_BUILTIN_TEMPLATE_DIR: Path = Path(".")
_CUSTOM_TEMPLATE_DIR: Path = Path(".")
_ROUND_TEMPLATE_PATHS: list[Path] = []
_SQUARE_TEMPLATE_PATHS: list[Path] = []
_TEMPLATE_MAP_LOCK = threading.Lock()
_TEMPLATE_MAP_CACHE: dict[str, IconTemplate] | None = None
_TEMPLATE_MAP_CACHE_KEY: tuple[str, str, int, int] | None = None


def configure_template_sources(
    *,
    builtin_dir: Path,
    custom_dir: Path,
    round_paths: list[Path],
    square_paths: list[Path],
) -> None:
    global _BUILTIN_TEMPLATE_DIR, _CUSTOM_TEMPLATE_DIR, _ROUND_TEMPLATE_PATHS, _SQUARE_TEMPLATE_PATHS
    global _TEMPLATE_MAP_CACHE, _TEMPLATE_MAP_CACHE_KEY
    changed = (
        _BUILTIN_TEMPLATE_DIR != builtin_dir
        or _CUSTOM_TEMPLATE_DIR != custom_dir
        or _ROUND_TEMPLATE_PATHS != list(round_paths)
        or _SQUARE_TEMPLATE_PATHS != list(square_paths)
    )
    _BUILTIN_TEMPLATE_DIR = builtin_dir
    _CUSTOM_TEMPLATE_DIR = custom_dir
    _ROUND_TEMPLATE_PATHS = list(round_paths)
    _SQUARE_TEMPLATE_PATHS = list(square_paths)
    if changed:
        with _TEMPLATE_MAP_LOCK:
            _TEMPLATE_MAP_CACHE = None
            _TEMPLATE_MAP_CACHE_KEY = None


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
    round_path = next((p for p in _ROUND_TEMPLATE_PATHS if p.exists()), None)
    square_path = next((p for p in _SQUARE_TEMPLATE_PATHS if p.exists()), None)
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
    _CUSTOM_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    return _discover_templates_from_dir(_CUSTOM_TEMPLATE_DIR)


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
    return _discover_templates_from_dir(_BUILTIN_TEMPLATE_DIR)


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
    cache_key = (
        str(_BUILTIN_TEMPLATE_DIR),
        str(_CUSTOM_TEMPLATE_DIR),
        _dir_mtime_ns(_BUILTIN_TEMPLATE_DIR),
        _dir_mtime_ns(_CUSTOM_TEMPLATE_DIR),
    )
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


def _read_config_field(config: object, key: str, fallback: object) -> object:
    if isinstance(config, dict):
        return config.get(key, fallback)
    return getattr(config, key, fallback)


def normalize_border_shader_config(
    config: BorderShaderConfig | dict[str, object] | object | None,
) -> BorderShaderConfig:
    if config is None:
        return BorderShaderConfig()
    mode_raw = str(_read_config_field(config, "mode", "hsv")).strip().casefold() or "hsv"
    mode = mode_raw if mode_raw in {"hsv", "hsl"} else "hsv"
    return BorderShaderConfig(
        enabled=bool(_read_config_field(config, "enabled", False)),
        mode=mode,
        hue=max(0, min(359, int(_read_config_field(config, "hue", 0) or 0))),
        saturation=max(0, min(100, int(_read_config_field(config, "saturation", 100) or 100))),
        tone=max(0, min(100, int(_read_config_field(config, "tone", 100) or 100))),
        intensity=max(0, min(100, int(_read_config_field(config, "intensity", 0) or 0))),
    )


def border_shader_to_dict(
    config: BorderShaderConfig | dict[str, object] | object | None,
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


def _fit_to_square(image: Image.Image, size: int) -> Image.Image:
    fitted = ImageOps.contain(image, (size, size), method=Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(
        fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2)
    )
    return out


def _apply_border_shader(
    overlay: Image.Image,
    border_shader: BorderShaderConfig | dict[str, object] | object | None,
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


def apply_template_overlay(
    image: Image.Image,
    size: int,
    template: IconTemplate,
    border_shader: BorderShaderConfig | dict[str, object] | object | None = None,
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


def template_interior_mask(template: IconTemplate, size: int) -> Image.Image | None:
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
    mask = template_interior_mask(template, size)
    if mask is None:
        return None
    out = BytesIO()
    mask.save(out, format="PNG")
    return out.getvalue()


def build_template_overlay_preview(
    icon_style: str | None,
    size: int = 256,
    border_shader: BorderShaderConfig | dict[str, object] | object | None = None,
) -> bytes:
    template = resolve_icon_template(icon_style, circular_ring=False)
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rendered = apply_template_overlay(base, size, template, border_shader=border_shader)
    out = BytesIO()
    rendered.save(out, format="PNG")
    return out.getvalue()


def build_composited_icon(
    master: Image.Image,
    size: int,
    template: IconTemplate,
    foreground: Image.Image | None = None,
    border_shader: BorderShaderConfig | dict[str, object] | object | None = None,
) -> Image.Image:
    if template.template_id == "none":
        base = _fit_to_square(master, size)
    else:
        source = _fit_to_square(master, size)
        base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        interior_mask = template_interior_mask(template, size)
        if interior_mask is not None:
            # Keep outside area transparent; black-fill only within template interior.
            black_fill = Image.new("RGBA", (size, size), (0, 0, 0, 255))
            base.paste(black_fill, (0, 0), interior_mask)
            src_alpha = source.getchannel("A")
            combined_mask = ImageChops.multiply(src_alpha, interior_mask)
            base.paste(source, (0, 0), combined_mask)
        else:
            base.alpha_composite(source)
    base = apply_template_overlay(base, size, template, border_shader=border_shader)
    if foreground is not None:
        base = Image.alpha_composite(base, _fit_to_square(foreground, size))
    return base

